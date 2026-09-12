# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""``agent-memory event write``: the port of the kernel's ``write_event.py`` and the writer contract on top.

The first section carries the kernel's ``tests/test_write_event.py`` cases,
one for one under their original names (14: nine for ``normalize_related``,
three for the ``related`` frontmatter line, two dry runs through the CLI).
The rest is what the port adds: byte parity with the script, the publication
contract (idempotent retry, refused collision, clean failure), the plain-scalar
grammar the frontmatter needs, and the verb's CLI surface.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_memory import events as ev
from agent_memory import publication
from agent_memory.cli import main
from agent_memory.home import ENV_COMPAT_HOME, ENV_HOME

GOLDEN = Path(__file__).resolve().parent / "golden" / "event_record.md"

NOW = dt.datetime(2026, 3, 21, 12, 0, 0, tzinfo=dt.timezone.utc)
BODY = b"# Decision\n\nUse the thing."

# What the kernel script composes for the minimal inputs the CLI tests use.
MINIMAL = b"---\ncreated_at_utc: 2026-03-21T12:00:00Z\ndate: 2026-03-21\nagent: claude\nproject: test\ntype: event\n---\n\nhello\n"


def call(home: Path, *, body: bytes = BODY, slug: str = "api-convention", **kw):
    params = dict(
        home=home,
        agent="alice",
        project="demo-project",
        event_type="decision",
        slug=slug,
        body=body,
        source_ref="20260321-alice-1f3a9c2b",
        related=["PR #43", "issue #10"],
        supersedes="events/20260316-120000-old-convention.md",
        now=NOW,
    )
    params.update(kw)
    return ev.write_event(**params)


def expected_path(home: Path, slug: str = "api-convention") -> Path:
    return home / "org-memory" / "events" / f"20260321-120000-{slug}.md"


def cli_args(home: Path, *extra: str) -> list:
    return [
        "event", "write",
        "--agent", "claude", "--project", "test", "--type", "event", "--slug", "test-slug",
        "--home", str(home), *extra,
    ]


def minimal_path(home: Path) -> Path:
    return home / "org-memory" / "events" / "20260321-120000-test-slug.md"


@pytest.fixture
def fixed_clock(monkeypatch):
    """Pin the verb's clock so a CLI run names and stamps the record deterministically."""
    monkeypatch.setattr(ev, "_as_utc", lambda now: NOW)


# ============================================================ ported: kernel tests/test_write_event.py ===

# --- TestNormalizeRelated (9) ---


def test_comma_separated():
    assert ev.normalize_related("PR #43, event/20260316-foo") == ["PR #43", "event/20260316-foo"]


def test_single_item():
    assert ev.normalize_related("PR #43") == ["PR #43"]


def test_json_array():
    assert ev.normalize_related('["PR #13", "PR #14"]') == ["PR #13", "PR #14"]


def test_json_single_item_array():
    assert ev.normalize_related('["PR #13"]') == ["PR #13"]


def test_json_empty_array():
    assert ev.normalize_related("[]") == []


def test_malformed_json_falls_back_to_comma_split():
    # Starts with '[' but is not valid JSON: falls back to the comma split.
    assert ev.normalize_related("[broken, json") == ["[broken", "json"]


def test_strips_whitespace():
    assert ev.normalize_related("  PR #1 ,  PR #2  ") == ["PR #1", "PR #2"]


def test_json_with_leading_whitespace():
    assert ev.normalize_related('  ["PR #1"]  ') == ["PR #1"]


def test_skips_empty_items():
    assert ev.normalize_related("PR #1,,, PR #2,") == ["PR #1", "PR #2"]


# --- TestBuildEventRelated (3) ---


def _build(related=None) -> str:
    return ev.compose_record(
        agent="claude", project="test", event_type="event", created_at=NOW, body=b"test body", related=related
    ).decode("utf-8")


def test_no_related_field():
    assert "related:" not in _build(related=None)


def test_plain_items():
    assert 'related: ["PR #43", "issue #10"]' in _build(related=["PR #43", "issue #10"])


def test_no_double_quoting():
    """Pre-parsed JSON items are not quoted a second time."""
    content = _build(related=["PR #13"])
    assert 'related: ["PR #13"]' in content
    assert '["[' not in content


# --- TestBuildEventDryRun (2): end to end through the CLI; the preview goes to stderr in this package ---


def test_dry_run_json_related(tmp_path, capsys):
    assert main(cli_args(tmp_path, "--body", "hello", "--related", '["PR #13", "PR #14"]', "--dry-run")) == 0
    preview = capsys.readouterr().err
    assert 'related: ["PR #13", "PR #14"]' in preview
    assert '["[' not in preview


def test_dry_run_comma_related(tmp_path, capsys):
    assert main(cli_args(tmp_path, "--body", "hello", "--related", "PR #13, PR #14", "--dry-run")) == 0
    assert 'related: ["PR #13", "PR #14"]' in capsys.readouterr().err


# ================================================================================ byte parity ===


def test_record_is_byte_identical_to_the_script_golden(tmp_path):
    """The record the kernel script composes for these inputs, captured once; the port must match it."""
    golden = GOLDEN.read_bytes()
    assert call(tmp_path, dry_run=True).record == golden
    result = call(tmp_path)
    assert result.status == "published"
    assert result.path.read_bytes() == golden


def test_minimal_record_matches_the_script_bytes(tmp_path):
    result = ev.write_event(
        home=tmp_path, agent="claude", project="test", event_type="event", slug="test-slug", body=b"hello", now=NOW
    )
    assert result.record == MINIMAL
    assert result.path == minimal_path(tmp_path)
    assert result.path.read_bytes() == MINIMAL


def test_publishes_at_the_canonical_path(tmp_path):
    target, status, created_at_utc, _ = call(tmp_path)
    assert status == "published"
    assert target == expected_path(tmp_path)
    assert target.is_file()
    assert created_at_utc == "2026-03-21T12:00:00Z"


def test_a_naive_clock_is_taken_as_utc_and_an_aware_one_is_converted(tmp_path):
    naive = call(tmp_path, dry_run=True, now=dt.datetime(2026, 3, 21, 12, 0, 0))
    eastward = call(tmp_path, dry_run=True, now=NOW.astimezone(dt.timezone(dt.timedelta(hours=9))))
    assert naive.record == eastward.record == GOLDEN.read_bytes()


# ============================================================================ frontmatter parity ===


def test_frontmatter_parses_back_in_canonical_order(tmp_path):
    parsed, body = ev.parse_record(call(tmp_path, dry_run=True).record)
    assert list(parsed) == [
        "created_at_utc", "date", "agent", "project", "type", "source_ref", "related", "supersedes",
    ]
    assert parsed["created_at_utc"] == "2026-03-21T12:00:00Z"
    assert parsed["date"] == "2026-03-21"
    assert parsed["agent"] == "alice"
    assert parsed["project"] == "demo-project"
    assert parsed["type"] == "decision"
    assert parsed["source_ref"] == "20260321-alice-1f3a9c2b"
    assert parsed["related"] == '["PR #43", "issue #10"]'
    assert parsed["supersedes"] == "events/20260316-120000-old-convention.md"
    assert body == BODY


def test_optional_fields_are_omitted_when_absent():
    parsed, body = ev.parse_record(MINIMAL)
    assert list(parsed) == list(ev.REQUIRED_FRONTMATTER_ORDER)
    assert body == b"hello"


@pytest.mark.parametrize(
    "tamper",
    [
        (b"date: 2026-03-21\n", b"date: 2026-03-22\n"),  # date disagrees with the stamp
        (b"type: decision\n", b""),  # a required field missing
        (b"type: decision\n", b"type: memo\n"),  # a type outside the enum
        (b'related: ["PR #43", "issue #10"]\n', b"related: [1, 2]\n"),  # not an array of strings
        (b'related: ["PR #43", "issue #10"]\n', b'related: ["PR #43","issue #10"]\n'),  # not the bytes written
        (b"supersedes: events/", b"related: x\nsupersedes: events/"),  # duplicate / misordered optional
        (b"---\n\n# Decision", b"---\n# Decision"),  # the blank line before the body missing
    ],
)
def test_verify_refuses_a_record_whose_frontmatter_does_not_read_back(tmp_path, tamper):
    record = call(tmp_path, dry_run=True).record
    old, new = tamper
    assert old in record
    with pytest.raises(ev.WriterError):
        ev.verify_record(record.replace(old, new, 1))


def test_identity_values_survive_a_real_yaml_parser(tmp_path):
    yaml = pytest.importorskip("yaml")
    record = call(tmp_path, dry_run=True).record.decode("utf-8")
    head = record.split("---\n")[1]
    loaded = yaml.safe_load(head)
    assert loaded["agent"] == "alice"
    assert loaded["project"] == "demo-project"
    assert loaded["type"] == "decision"
    assert loaded["source_ref"] == "20260321-alice-1f3a9c2b"
    assert loaded["related"] == ["PR #43", "issue #10"]
    assert loaded["supersedes"] == "events/20260316-120000-old-convention.md"


# ============================================================================= write contract ===


def test_retry_after_success_is_idempotent(tmp_path):
    first_target, first_status, _, _ = call(tmp_path)
    assert first_status == "published"
    stamp = first_target.stat().st_mtime_ns

    second_target, second_status, _, _ = call(tmp_path)
    assert second_status == "idempotent"
    assert second_target == first_target
    assert second_target.stat().st_mtime_ns == stamp, "record must not be rewritten"
    assert list(first_target.parent.glob(".stage.*")) == []


def test_differing_record_at_the_same_name_is_refused(tmp_path):
    target, _, _, _ = call(tmp_path)
    original = target.read_bytes()

    with pytest.raises(ev.WriterError, match="different record") as excinfo:
        call(tmp_path, body=b"# Decision\n\nUse the other thing.")
    assert excinfo.value.code == 2

    assert target.read_bytes() == original, "a published event is never replaced"
    assert sorted(path.name for path in target.parent.iterdir()) == [target.name]


def test_failure_before_the_link_leaves_events_clean(tmp_path, monkeypatch):
    def boom(*_a, **_kw):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "link", boom)
    with pytest.raises(OSError):
        call(tmp_path)

    target = expected_path(tmp_path)
    assert not target.exists()
    assert list(target.parent.iterdir()) == [], "events/ holds no partial record and no staging debris"


def test_short_write_before_publish_leaves_events_clean(tmp_path, monkeypatch):
    real_write = os.write

    def truncating_write(fd, data):
        if isinstance(data, bytes) and data.startswith(b"---\n") and len(data) > 100:
            return real_write(fd, data[: len(data) // 2])
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", truncating_write)
    with pytest.raises(ev.WriterError):
        call(tmp_path)

    target = expected_path(tmp_path)
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_publication_runs_the_frontmatter_verifier(tmp_path, monkeypatch):
    seen = []

    def spy(raw: bytes) -> None:
        seen.append(raw)
        ev.parse_record(raw)

    monkeypatch.setattr(ev, "verify_record", spy)
    result = call(tmp_path)
    assert result.status == "published"
    assert seen and all(raw == result.record for raw in seen), "staged bytes and read-back both pass through it"


def test_a_symlink_at_the_canonical_path_is_refused(tmp_path):
    target = expected_path(tmp_path)
    target.parent.mkdir(parents=True)
    decoy = tmp_path / "elsewhere.md"
    decoy.write_bytes(b"not an event\n")
    target.symlink_to(decoy)

    with pytest.raises(publication.WriterError, match="symlink"):
        call(tmp_path)
    assert decoy.read_bytes() == b"not an event\n"
    assert target.is_symlink()


def test_dry_run_composes_the_real_record_and_writes_nothing(tmp_path):
    result = call(tmp_path, dry_run=True)
    assert result.status == "dry-run"
    assert result.path == expected_path(tmp_path)
    assert result.record == GOLDEN.read_bytes()
    assert not (tmp_path / "org-memory").exists(), "the store is not even created"


def test_dry_run_then_publish_lands_the_previewed_bytes(tmp_path):
    previewed = call(tmp_path, dry_run=True).record
    published = call(tmp_path)
    assert published.status == "published"
    assert published.path.read_bytes() == previewed


# ================================================================================= validation ===


@pytest.mark.parametrize("slug", ["", "-a", "a-", "A", "v0.2.3", "a b", "a_b", "a" * 65])
def test_invalid_slug_is_rejected_before_anything_is_written(tmp_path, slug):
    with pytest.raises(ev.WriterError, match="slug") as excinfo:
        call(tmp_path, slug=slug)
    assert excinfo.value.code == 1
    assert not (tmp_path / "org-memory").exists()


@pytest.mark.parametrize("slug", ["a", "a-b", "0-2-3", "x" * 64])
def test_slug_grammar_boundaries_are_accepted(tmp_path, slug):
    assert call(tmp_path, slug=slug, dry_run=True).path.name == f"20260321-120000-{slug}.md"


def test_invalid_type_is_rejected(tmp_path):
    with pytest.raises(ev.WriterError, match="type") as excinfo:
        call(tmp_path, event_type="memo")
    assert excinfo.value.code == 1
    assert not (tmp_path / "org-memory").exists()


@pytest.mark.parametrize(
    "field, value",
    [
        ("agent", "al\nice"),
        ("project", "de\x00mo"),
        ("source_ref", "a\tb"),
        ("supersedes", "x\nsupersedes: y"),
    ],
)
def test_control_characters_cannot_inject_frontmatter(tmp_path, field, value):
    with pytest.raises(ev.WriterError) as excinfo:
        call(tmp_path, **{field: value})
    assert excinfo.value.code == 1
    assert not (tmp_path / "org-memory").exists()


@pytest.mark.parametrize("value", ["true", "Null", "y", "12", "1.0", "1e3", "0x1f", "0o7", "1_000", "1:30", "2026-03-21"])
def test_a_value_a_yaml_reader_would_retype_is_refused(tmp_path, value):
    """The frontmatter is plain scalars by contract, so it refuses what it cannot carry as a string."""
    with pytest.raises(ev.WriterError, match="read back as") as excinfo:
        call(tmp_path, source_ref=value)
    assert excinfo.value.code == 1


@pytest.mark.parametrize("value", ["- item", "[a]", "{a}", "a: b", "a #b", "trailing:", '"quoted"', "'quoted'", "*ref"])
def test_a_value_a_yaml_reader_would_read_as_structure_is_refused(tmp_path, value):
    with pytest.raises(ev.WriterError, match="plain YAML scalar") as excinfo:
        call(tmp_path, supersedes=value)
    assert excinfo.value.code == 1


@pytest.mark.parametrize("value", [" padded", "padded ", ""])
def test_an_untrimmed_or_empty_scalar_is_refused(tmp_path, value):
    with pytest.raises(ev.WriterError) as excinfo:
        call(tmp_path, agent=value)
    assert excinfo.value.code == 1


def test_a_scalar_past_the_length_cap_is_refused(tmp_path):
    with pytest.raises(ev.WriterError, match="longer than"):
        call(tmp_path, source_ref="x" * (ev.MAX_SCALAR_CHARS + 1))


@pytest.mark.parametrize("item", ['PR "43"', "back\\slash", "a\nb"])
def test_a_related_item_that_breaks_the_array_is_refused(tmp_path, item):
    with pytest.raises(ev.WriterError, match="related item") as excinfo:
        call(tmp_path, related=[item])
    assert excinfo.value.code == 1


@pytest.mark.parametrize("agent", ["-lead", "a/b", "alice bob"])
def test_agent_grammar_violations_are_rejected(tmp_path, agent):
    with pytest.raises(ev.WriterError, match="agent") as excinfo:
        call(tmp_path, agent=agent)
    assert excinfo.value.code == 1


@pytest.mark.parametrize("project", [".hidden", "a/b", "a\\b"])
def test_project_grammar_violations_are_rejected(tmp_path, project):
    with pytest.raises(ev.WriterError, match="project") as excinfo:
        call(tmp_path, project=project)
    assert excinfo.value.code == 1


def test_empty_body_is_rejected(tmp_path):
    with pytest.raises(ev.WriterError, match="empty body") as excinfo:
        call(tmp_path, body=b"")
    assert excinfo.value.code == 1
    assert not (tmp_path / "org-memory").exists()


def test_a_body_that_is_not_utf8_is_rejected(tmp_path):
    with pytest.raises(ev.WriterError, match="UTF-8") as excinfo:
        call(tmp_path, body=b"\xff\xfe")
    assert excinfo.value.code == 1
    assert not (tmp_path / "org-memory").exists()


def test_multibyte_utf8_bodies_round_trip_byte_for_byte(tmp_path):
    body = "# Sesión\n\nDécision — 決定 🎉".encode("utf-8")
    result = call(tmp_path, body=body)
    assert result.path.read_bytes().endswith(b"\n" + body + b"\n")
    assert ev.parse_record(result.path.read_bytes())[1] == body


# ======================================================================================== CLI ===


def test_cli_publishes_and_reports_json(tmp_path, capsys, fixed_clock):
    assert main(cli_args(tmp_path, "--body", "hello", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert list(payload) == ["path", "status", "created_at_utc", "date", "agent", "project", "type"]
    assert payload["status"] == "published"
    assert payload["created_at_utc"] == "2026-03-21T12:00:00Z"
    assert payload["date"] == "2026-03-21"
    assert (payload["agent"], payload["project"], payload["type"]) == ("claude", "test", "event")
    assert Path(payload["path"]) == minimal_path(tmp_path)
    assert minimal_path(tmp_path).read_bytes() == MINIMAL


def test_cli_text_output_names_status_path_and_stamp(tmp_path, capsys, fixed_clock):
    assert main(cli_args(tmp_path, "--body", "hello")) == 0
    assert capsys.readouterr().out == f"published: {minimal_path(tmp_path)}\ncreated_at_utc: 2026-03-21T12:00:00Z\n"


def test_cli_stamps_the_current_clock(tmp_path, capsys):
    before = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    assert main(cli_args(tmp_path, "--body", "hello", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    stamped = dt.datetime.strptime(payload["created_at_utc"], ev.TIMESTAMP_FORMAT).replace(tzinfo=dt.timezone.utc)
    assert before <= stamped <= dt.datetime.now(dt.timezone.utc)
    assert Path(payload["path"]).name == f"{stamped.strftime('%Y%m%d-%H%M%S')}-test-slug.md"


def test_cli_reads_the_body_from_a_file_and_drops_trailing_newlines(tmp_path, capsys, fixed_clock):
    body_file = tmp_path / "body.md"
    body_file.write_bytes(b"hello\n\n")
    assert main(cli_args(tmp_path, "--body-file", str(body_file))) == 0
    assert minimal_path(tmp_path).read_bytes() == MINIMAL


# The kernel script reads a body FILE as text (universal newlines: CRLF and CR
# become LF) and takes stdin and --body as given; probed against the script at
# oacp-dev 9fe2f76 on 2026-09-10: file CRLF -> 'First line\nSecond line', stdin
# CRLF -> 'A\r\nB\r\n\r', inline CRLF -> 'a\r\nb\r'. Round-1 F-001 (codex).
KERNEL_CRLF_FILE = b"First line\r\nSecond line\r\n\r\n"
KERNEL_CRLF_FILE_BODY = b"First line\nSecond line"


@pytest.mark.parametrize(
    "raw, body",
    [
        (KERNEL_CRLF_FILE, KERNEL_CRLF_FILE_BODY),
        (b"One\rTwo\r", b"One\nTwo"),
        (b"a\r\r\nb\n", b"a\n\nb"),
        (b"lf only\n\n", b"lf only"),
        (b"no newline", b"no newline"),
        ("bom \ufeffkept".encode("utf-8"), "bom \ufeffkept".encode("utf-8")),
    ],
)
def test_body_from_file_reads_like_the_kernel_text_read(raw, body):
    assert ev.body_from_file(raw) == body


def test_body_from_file_refuses_a_file_that_is_not_utf8():
    with pytest.raises(ev.WriterError, match="UTF-8") as excinfo:
        ev.body_from_file(b"First\r\n\xff\xfe")
    assert excinfo.value.code == 1


def test_cli_crlf_body_file_produces_the_kernel_record_bytes(tmp_path, capsys, fixed_clock):
    body_file = tmp_path / "windows-body.md"
    body_file.write_bytes(KERNEL_CRLF_FILE)
    assert main(cli_args(tmp_path, "--body-file", str(body_file))) == 0
    assert minimal_path(tmp_path).read_bytes() == MINIMAL.replace(b"\nhello\n", b"\n" + KERNEL_CRLF_FILE_BODY + b"\n")
    assert b"\r" not in minimal_path(tmp_path).read_bytes()


def test_cli_cr_body_file_produces_the_kernel_record_bytes(tmp_path, capsys, fixed_clock):
    body_file = tmp_path / "classic-mac-body.md"
    body_file.write_bytes(b"hello\r")
    assert main(cli_args(tmp_path, "--body-file", str(body_file))) == 0
    assert minimal_path(tmp_path).read_bytes() == MINIMAL


def test_cli_crlf_body_file_and_its_lf_twin_land_the_same_record(tmp_path, capsys, fixed_clock):
    crlf = tmp_path / "crlf.md"
    crlf.write_bytes(b"hello\r\n")
    lf = tmp_path / "lf.md"
    lf.write_bytes(b"hello\n")
    assert main(cli_args(tmp_path, "--body-file", str(crlf))) == 0
    assert capsys.readouterr().out.startswith("published:")
    assert main(cli_args(tmp_path, "--body-file", str(lf))) == 0
    assert capsys.readouterr().out.startswith("idempotent:"), "the LF twin is the same record, not a collision"


def test_cli_non_utf8_body_file_is_refused_before_the_store_is_touched(tmp_path, capsys):
    body_file = tmp_path / "latin1.md"
    body_file.write_bytes(b"caf\xe9\r\n")
    assert main(cli_args(tmp_path, "--body-file", str(body_file))) == 1
    assert "UTF-8" in capsys.readouterr().err
    assert not (tmp_path / "org-memory").exists()


def test_cli_stdin_keeps_carriage_returns_like_the_kernel(tmp_path, capsys, monkeypatch, fixed_clock):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"A\r\nB\r\n\r\n")))
    assert main(cli_args(tmp_path, "--body-file", "-")) == 0
    assert ev.parse_record(minimal_path(tmp_path).read_bytes())[1] == b"A\r\nB\r\n\r"


def test_cli_inline_body_keeps_carriage_returns_like_the_kernel(tmp_path, capsys, fixed_clock):
    assert main(cli_args(tmp_path, "--body", "a\r\nb\r\n")) == 0
    assert ev.parse_record(minimal_path(tmp_path).read_bytes())[1] == b"a\r\nb\r"


def test_cli_reads_the_body_from_stdin_with_a_dash(tmp_path, capsys, monkeypatch, fixed_clock):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"hello\n")))
    assert main(cli_args(tmp_path, "--body-file", "-")) == 0
    assert minimal_path(tmp_path).read_bytes() == MINIMAL


def test_cli_reads_piped_stdin_when_no_body_flag_is_given(tmp_path, capsys, monkeypatch, fixed_clock):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"hello\n")))
    assert main(cli_args(tmp_path)) == 0
    assert minimal_path(tmp_path).read_bytes() == MINIMAL


def test_cli_without_a_body_is_a_validation_error(tmp_path, capsys, monkeypatch):
    class Terminal:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", Terminal())
    assert main(cli_args(tmp_path)) == 1
    assert "no body provided" in capsys.readouterr().err
    assert not (tmp_path / "org-memory").exists()


def test_cli_body_and_body_file_are_mutually_exclusive(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(cli_args(tmp_path, "--body", "a", "--body-file", "b.md"))
    assert exit_info.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_cli_returns_one_on_an_unreadable_body_file(tmp_path, capsys):
    assert main(cli_args(tmp_path, "--body-file", str(tmp_path / "missing.md"))) == 1
    assert "cannot read body" in capsys.readouterr().err
    assert not (tmp_path / "org-memory").exists()


def test_cli_returns_one_on_a_validation_error(tmp_path, capsys):
    args = cli_args(tmp_path, "--body", "hello")
    args[args.index("test-slug")] = "v0.2.3"
    assert main(args) == 1
    assert "slug" in capsys.readouterr().err
    assert not (tmp_path / "org-memory").exists()


def test_cli_rejects_a_type_outside_the_enum(tmp_path, capsys):
    args = cli_args(tmp_path, "--body", "hello")
    args[args.index("event")] = "memo"
    with pytest.raises(SystemExit) as exit_info:
        main(args)
    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_identical_rerun_exits_zero_and_leaves_the_record_unchanged(tmp_path, capsys, fixed_clock):
    assert main(cli_args(tmp_path, "--body", "hello")) == 0
    stamp = minimal_path(tmp_path).stat().st_mtime_ns
    assert main(cli_args(tmp_path, "--body", "hello")) == 0
    assert capsys.readouterr().out.endswith(f"idempotent: {minimal_path(tmp_path)}\ncreated_at_utc: 2026-03-21T12:00:00Z\n")
    assert minimal_path(tmp_path).stat().st_mtime_ns == stamp
    assert minimal_path(tmp_path).read_bytes() == MINIMAL


def test_cli_returns_two_on_a_differing_record_at_the_same_name(tmp_path, capsys, fixed_clock):
    assert main(cli_args(tmp_path, "--body", "hello")) == 0
    assert main(cli_args(tmp_path, "--body", "goodbye")) == 2
    assert "different record" in capsys.readouterr().err
    assert minimal_path(tmp_path).read_bytes() == MINIMAL


def test_cli_renders_every_optional_field(tmp_path, capsys, fixed_clock):
    args = [
        "event", "write", "--agent", "alice", "--project", "demo-project", "--type", "decision",
        "--slug", "api-convention", "--body", BODY.decode("utf-8"),
        "--source-ref", "20260321-alice-1f3a9c2b", "--related", "PR #43, issue #10",
        "--supersedes", "events/20260316-120000-old-convention.md", "--home", str(tmp_path),
    ]
    assert main(args) == 0
    assert expected_path(tmp_path).read_bytes() == GOLDEN.read_bytes()


def test_cli_accepts_the_hidden_oacp_dir_alias(tmp_path, capsys, fixed_clock):
    store = tmp_path / "store"
    args = cli_args(store, "--body", "hello", "--json")
    args[args.index("--home")] = "--oacp-dir"
    assert main(args) == 0
    assert Path(json.loads(capsys.readouterr().out)["path"]) == minimal_path(store)
    with pytest.raises(SystemExit) as exit_info:
        main(["event", "write", "--help"])
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--home" in help_text and "--oacp-dir" not in help_text


def test_cli_resolves_the_home_like_every_other_verb(tmp_path, capsys, monkeypatch, fixed_clock):
    store = tmp_path / "env-store"
    monkeypatch.setenv(ENV_HOME, str(store))
    monkeypatch.delenv(ENV_COMPAT_HOME, raising=False)
    args = cli_args(store, "--body", "hello", "--json")
    del args[args.index("--home") : args.index("--home") + 2]
    assert main(args) == 0
    assert Path(json.loads(capsys.readouterr().out)["path"]) == minimal_path(store)


def test_cli_dry_run_writes_nothing_and_reports_the_status(tmp_path, capsys, fixed_clock):
    assert main(cli_args(tmp_path, "--body", "hello", "--dry-run", "--json")) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "dry-run"
    assert captured.err.endswith(MINIMAL.decode("utf-8")), "the composed record is shown for review"
    assert not (tmp_path / "org-memory").exists()


def test_event_without_a_subcommand_shows_help(capsys):
    assert main(["event"]) == 2
    assert "usage: agent-memory" in capsys.readouterr().err


# ------------------------------------------------------------ installed verb ---


def test_the_verb_runs_from_an_unrelated_working_directory(tmp_path):
    """The console script, from a working directory unrelated to the home, with no kernel and no env var."""
    project = tmp_path / "elsewhere"
    project.mkdir()
    body_file = project / "body.md"
    body_file.write_bytes(b"hello\n")
    store = tmp_path / "store"
    env = {key: value for key, value in os.environ.items() if key not in (ENV_HOME, ENV_COMPAT_HOME)}
    args = [sys.executable, "-m", "agent_memory", *cli_args(store, "--body-file", str(body_file), "--json")]

    dry = subprocess.run(args + ["--dry-run"], cwd=project, capture_output=True, text=True, env=env)
    assert dry.returncode == 0, dry.stderr
    assert '"status": "dry-run"' in dry.stdout
    assert not (store / "org-memory").exists()

    real = subprocess.run(args, cwd=project, capture_output=True, text=True, env=env)
    assert real.returncode == 0, real.stderr
    payload = json.loads(real.stdout)
    assert payload["status"] == "published"
    assert Path(payload["path"]).read_bytes().endswith(b"---\n\nhello\n")
