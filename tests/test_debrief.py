# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Conformance tests for the debrief writer, ported whole from the skill script's suite.

The memory layout spec ("Writer commit contract") requires writer conformance
tests to pin seven behaviors. Each is marked below with the ``CONFORMANCE``
tag naming the mandated case:

1. exception / short write before publish -> canonical path stays absent
2. interrupted publication recovery
3. read-back mismatch
4. retry after success (idempotent)
5. differing-content collision
6. symlink at target
7. concurrent identical and differing writers

Beyond those, two classes are pinned because nothing downstream can catch
them: staging ownership (the writer must only ever publish an inode it
created itself) and frontmatter serialization (a record must say what it
was asked to say when a real YAML parser reads it back). The port adds the
golden (byte-identical to the script's record), the doctor's acceptance of
the produced layout, the hidden ``--oacp-dir`` alias, and the verb run from
the installed console script in an unrelated working directory.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agent_memory import debrief as wd
from agent_memory import org, publication
from agent_memory.cli import main
from agent_memory.home import ENV_COMPAT_HOME, ENV_HOME

GOLDEN = Path(__file__).resolve().parent / "golden" / "debrief_record.md"

STARTED = "2026-08-25T20:04:11Z"
ENDED = "2026-08-25T22:01:47Z"
BODY = b"# Session\n\nDid the thing.\n"


def call(home: Path, *, body: bytes = BODY, session: str = "1f3a9c2b", **kw):
    params = dict(
        home=home,
        project="demo-project",
        agent="alice",
        runtime="claude",
        session=session,
        started_utc=STARTED,
        ended_utc=ENDED,
        body=body,
    )
    params.update(kw)
    return wd.write_debrief(**params)


def expected_path(home: Path, session: str = "1f3a9c2b") -> Path:
    return home / "org-memory" / "debriefs" / "demo-project" / "2026" / "08" / f"20260825-alice-{session}.md"


def cli_args(home: Path, body_file: Path, *extra: str) -> list:
    return [
        "debrief", "write",
        "--project", "demo-project", "--agent", "alice", "--runtime", "claude",
        "--session", "1f3a9c2b", "--started-utc", STARTED, "--ended-utc", ENDED,
        "--body-file", str(body_file), "--home", str(home), *extra,
    ]


# ---------------------------------------------------------------- golden ---


def test_record_is_byte_identical_to_the_script_golden(tmp_path):
    """The record the skill script composes for these inputs, captured once; the port must match it."""
    golden = GOLDEN.read_bytes()
    assert call(tmp_path, dry_run=True).record == golden
    result = call(tmp_path)
    assert result.status == "published"
    assert result.path.read_bytes() == golden


# ---------------------------------------------------------------- layout ---


def test_publishes_at_the_canonical_path(tmp_path):
    target, status, digest, _ = call(tmp_path)
    assert status == "published"
    assert target == expected_path(tmp_path)
    assert target.is_file()
    assert digest == wd.content_sha256(BODY)


def test_frontmatter_carries_every_required_field(tmp_path):
    target, _, _, _ = call(tmp_path)
    fm, body = wd.split_record(target.read_bytes())
    assert body == BODY
    for field in wd.REQUIRED_FRONTMATTER_ORDER:
        assert field in fm, f"missing required frontmatter field {field}"
    assert fm["schema_version"] == "1"
    assert fm["immutable"] == "true"
    assert fm["project"] == "demo-project"
    assert fm["agent"] == "alice"
    assert fm["runtime"] == "claude"
    assert fm["session"] == "1f3a9c2b"
    assert fm["started_utc"] == STARTED
    assert fm["ended_utc"] == ENDED


def test_content_hash_covers_the_exact_body_bytes(tmp_path):
    # Trailing whitespace and newlines are part of the body -- no normalization.
    body = b"# Session\n\ntrailing spaces   \n\n\n"
    target, _, _, _ = call(tmp_path, body=body)
    fm, stored = wd.split_record(target.read_bytes())
    assert stored == body
    assert fm["content_sha256"] == wd.content_sha256(body)


def test_body_containing_a_frontmatter_delimiter_round_trips(tmp_path):
    body = b"# Session\n\n---\n\nA horizontal rule lives here.\n"
    target, _, _, _ = call(tmp_path, body=body)
    fm, stored = wd.split_record(target.read_bytes())
    assert stored == body
    assert fm["content_sha256"] == wd.content_sha256(body)


def test_filename_matches_the_protocol_grammar(tmp_path):
    target, _, _, _ = call(tmp_path)
    grammar = re.compile(r"^(?P<date>\d{8})-(?P<agent>[A-Za-z0-9][A-Za-z0-9._-]{0,63})-(?P<session>[a-z0-9]{1,32})\.md$")
    match = grammar.match(target.name)
    assert match is not None
    # Session is the substring after the FINAL hyphen.
    assert target.name.rsplit("-", 1)[1] == "1f3a9c2b.md"
    # Directory segments agree with the filename date prefix.
    assert target.parent.name == match.group("date")[4:6]
    assert target.parent.parent.name == match.group("date")[0:4]


def test_hyphenated_agent_name_stays_parseable(tmp_path):
    target, _, _, _ = call(tmp_path, agent="bob-ops", session="9f00aa11")
    assert target.name == "20260825-bob-ops-9f00aa11.md"
    assert target.name.rsplit("-", 1)[1] == "9f00aa11.md"


def test_doctor_accepts_the_produced_layout(tmp_path, capsys):
    home = tmp_path / "home"
    org.init(home)
    assert call(home).status == "published"
    assert call(home, session="1f3a9c2c", body=b"# Second\n").status == "published"
    assert main(["doctor", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "2 debrief file(s)" in out and "canonical layout" in out


# ------------------------------------------------------------ validation ---


@pytest.mark.parametrize(
    "kwargs",
    [
        {"session": "has-hyphen"},
        {"session": "UPPERCASE"},
        {"session": "x" * 33},
        {"session": ""},
        {"agent": ".hidden"},
        {"agent": "bad/slash"},
        {"project": ".dotted"},
        {"project": "with/slash"},
        {"started_utc": "2026-08-25 20:04:11"},
        {"ended_utc": "not-a-date"},
        {"body": b""},
    ],
)
def test_invalid_input_is_rejected_before_anything_is_written(tmp_path, kwargs):
    with pytest.raises(wd.WriterError) as excinfo:
        call(tmp_path, **kwargs)
    assert excinfo.value.code == 1
    assert not (tmp_path / "org-memory").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("project", "demo\nimmutable: false\nx"),
        ("project", "demo\rx"),
        ("project", "demo\x00x"),
        ("agent", "alice\nrogue: true"),
        ("runtime", "claude\nimmutable: false"),
        ("runtime", ""),
        ("runtime", "has space"),
        ("session", "abc\ndef"),
    ],
)
def test_control_characters_cannot_inject_frontmatter(tmp_path, field, value):
    # A newline in any identity field would otherwise open a second frontmatter
    # line and let a caller forge fields such as immutable: false.
    with pytest.raises(wd.WriterError) as excinfo:
        call(tmp_path, **{field: value})
    assert excinfo.value.code == 1
    assert not (tmp_path / "org-memory").exists()


def test_quote_in_an_identity_field_cannot_break_the_frontmatter(tmp_path):
    # project's protocol rule permits an apostrophe; single-quoted YAML escapes
    # it by doubling, and the value must survive a round trip intact.
    target, _, _, _ = call(tmp_path, project="it's-a-project")
    fm, _ = wd.split_record(target.read_bytes())
    assert fm["project"] == "it's-a-project"
    assert fm["immutable"] == "true"
    assert fm["schema_version"] == "1"


def test_yaml_scalar_refuses_control_characters_directly(tmp_path):
    # Defense in depth: composition rejects even if validation is bypassed.
    with pytest.raises(wd.WriterError, match="control characters"):
        wd._yaml_scalar("demo\nimmutable: false")


def test_ended_before_started_is_rejected(tmp_path):
    with pytest.raises(wd.WriterError) as excinfo:
        call(tmp_path, started_utc=ENDED, ended_utc=STARTED)
    assert excinfo.value.code == 1
    assert not (tmp_path / "org-memory").exists()


# ---- CONFORMANCE 1: exception / short write before publish ----------------


def test_short_write_before_publish_leaves_canonical_absent(tmp_path, monkeypatch):
    real_write = os.write

    def truncating_write(fd, data):
        # Only truncate the record write itself; os.write is process-global and
        # the test runner's own output must pass through untouched.
        if isinstance(data, bytes) and data.startswith(b"---\n") and len(data) > 100:
            return real_write(fd, data[: len(data) // 2])
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", truncating_write)
    with pytest.raises(wd.WriterError):
        call(tmp_path)

    target = expected_path(tmp_path)
    assert not target.exists(), "canonical path must stay absent on a failed publish"
    leftovers = list(target.parent.glob(".stage.*"))
    assert leftovers == [], "failed publish must clean up its own staging artifact"


def test_exception_before_publish_leaves_canonical_absent(tmp_path, monkeypatch):
    def boom(*_a, **_kw):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "link", boom)
    with pytest.raises(OSError):
        call(tmp_path)

    target = expected_path(tmp_path)
    assert not target.exists()
    assert list(target.parent.glob(".stage.*")) == []


# ---- CONFORMANCE 2: interrupted publication recovery ----------------------


def _record(body: bytes = BODY, session: str = "1f3a9c2b") -> bytes:
    return wd.compose_record(
        project="demo-project", agent="alice", runtime="claude",
        session=session, started_utc=STARTED, ended_utc=ENDED, body=body,
    )


def _pin_staging_name(monkeypatch, target: Path, suffix: str = "pinned") -> Path:
    """Force the writer onto a known staging name.

    The real nonce is unpredictable, which is the primary defense: nothing can
    pre-create the path the writer is about to claim. Pinning it lets these
    tests exercise the defense *behind* that one -- the ownership checks that
    must hold even if an attacker could guess the name. The name is looked up
    in the publication module, where the staging happens.
    """
    pinned = target.with_name(f"{wd.STAGE_PREFIX}{target.name}.{suffix}")
    monkeypatch.setattr(publication, "staging_path", lambda _target: pinned)
    return pinned


def test_retry_after_an_interrupted_run_publishes_and_sweeps_the_stale_stage(tmp_path):
    # A crashed run left a complete stage behind. The retry publishes on its
    # own fresh inode and leaves no staging debris.
    target = expected_path(tmp_path)
    target.parent.mkdir(parents=True)
    record = _record()
    stale = target.with_name(f"{wd.STAGE_PREFIX}{target.name}.crashedrun")
    stale.write_bytes(record)

    assert call(tmp_path).status == "published"
    assert target.read_bytes() == record
    assert list(target.parent.glob(".stage.*")) == [], "no staging debris survives"


def test_retry_after_a_partial_run_publishes_and_sweeps_the_partial_stage(tmp_path):
    target = expected_path(tmp_path)
    target.parent.mkdir(parents=True)
    record = _record()
    partial = target.with_name(f"{wd.STAGE_PREFIX}{target.name}.crashedrun")
    partial.write_bytes(record[: len(record) // 3])

    assert call(tmp_path).status == "published"
    assert target.read_bytes() == record
    assert list(target.parent.glob(".stage.*")) == [], "no staging debris survives"


def test_the_sweep_never_removes_a_file_this_writer_does_not_own(tmp_path):
    # The sweep is scoped by construction to this record's staging prefix, but
    # it still refuses anything that is not a plain single-linked file of ours:
    # those were never this writer's to delete.
    target = expected_path(tmp_path)
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"not ours\n")

    symlinked = target.with_name(f"{wd.STAGE_PREFIX}{target.name}.symlink")
    symlinked.symlink_to(outside)
    multilinked = target.with_name(f"{wd.STAGE_PREFIX}{target.name}.multilink")
    multilinked.write_bytes(b"partial")
    os.link(multilinked, tmp_path / "someone-elses-name")
    subdir = target.with_name(f"{wd.STAGE_PREFIX}{target.name}.dir")
    subdir.mkdir()

    assert call(tmp_path).status == "published"
    survivors = sorted(p.name for p in target.parent.iterdir() if p.name.startswith(".stage."))
    assert survivors == [subdir.name, multilinked.name, symlinked.name]
    assert outside.read_bytes() == b"not ours\n"


def test_staging_name_is_writer_unique_and_private(tmp_path):
    target = expected_path(tmp_path)
    names = {wd.staging_path(target).name for _ in range(64)}
    assert len(names) == 64, "the nonce must be writer-unique, not derived from content"
    for name in names:
        assert name.startswith(".stage."), "staging name is outside the canonical namespace"
    assert wd.staging_path(target).parent == target.parent, "stage lives beside its target"


# ---- staging ownership: never publish an inode we did not create ----------


def test_a_symlink_at_the_staging_path_is_never_published(tmp_path, monkeypatch):
    # The attack this closes: pre-place a symlink where the writer will stage,
    # pointing at a file holding the exact record. Following it would publish a
    # canonical "immutable" record that stays mutable through the shared inode.
    target = expected_path(tmp_path)
    target.parent.mkdir(parents=True)
    record = _record()
    external = tmp_path / "attacker.bin"
    external.write_bytes(record)

    pinned = _pin_staging_name(monkeypatch, target)
    pinned.symlink_to(external)

    with pytest.raises(wd.WriterError, match="cannot create staging file"):
        call(tmp_path)

    assert not target.exists(), "canonical path stays absent"
    assert pinned.is_symlink(), "the foreign artifact is left for the operator"
    assert external.read_bytes() == record, "and is never written through"


def test_a_regular_file_at_the_staging_path_is_never_adopted(tmp_path, monkeypatch):
    target = expected_path(tmp_path)
    target.parent.mkdir(parents=True)
    record = _record()
    pinned = _pin_staging_name(monkeypatch, target)
    pinned.write_bytes(record)  # byte-identical, and still not ours

    with pytest.raises(wd.WriterError, match="cannot create staging file"):
        call(tmp_path)
    assert not target.exists()


def test_a_multi_link_staging_file_is_refused(tmp_path, monkeypatch):
    # O_EXCL guarantees a fresh inode, so this can only happen if something
    # hard-links the stage between the create and the publish. Publishing then
    # would leave the record reachable -- and writable -- under another name.
    target = expected_path(tmp_path)
    target.parent.mkdir(parents=True)
    shadow = tmp_path / "shadow"
    real_open = os.open

    def linking_open(path, flags, mode=0o777, **kw):
        fd = real_open(path, flags, mode, **kw)
        if str(path).startswith(str(target.parent / wd.STAGE_PREFIX)):
            os.link(path, shadow)
        return fd

    monkeypatch.setattr(os, "open", linking_open)
    with pytest.raises(wd.WriterError, match="links"):
        call(tmp_path)
    assert not target.exists(), "canonical path stays absent"


def test_publication_binds_to_the_inode_that_was_verified(tmp_path, monkeypatch):
    # If the canonical name is taken by some other file between the staged
    # verification and the link, the writer must report it rather than treat
    # whatever landed there as its own record.
    imposter = tmp_path / "imposter.md"
    imposter.write_bytes(_record())
    real_link = os.link

    def hijacking_link(src, dst, **kw):
        real_link(imposter, dst, **kw)

    monkeypatch.setattr(os, "link", hijacking_link)
    # Every attempt is hijacked, so the writer restages three times, takes the
    # imposter's name back down each time, and reports the exhausted retry.
    with pytest.raises(wd.WriterError, match="consecutive attempts"):
        call(tmp_path)
    assert not os.path.lexists(expected_path(tmp_path)), "the name the hijacked link created is taken back down"
    assert imposter.read_bytes() == _record(), "the imposter's own name is untouched"


# ---- the staging name swapped at the link (codex PR #19 r1, F-001) ---------
# Between the fstat that verified the stage and the link that publishes it, an
# actor with write access to the directory can turn the staging NAME into a
# hard link or a symlink to a foreign partial file. Foreign bytes are never the
# published record: bound to the descriptor, the link refuses the orphaned
# inode and nothing foreign is ever visible; bound to the name, the foreign
# file is visible from the link until the identity check takes the name back
# down (the narrower name-fallback contract, publication module docstring).
# Both read as a vanished stage: the writer restages and retries, so a one-shot
# swap ends in a clean publish and a persistent one in a reported failure with
# the canonical path absent.


def _swap_stage_at_link(monkeypatch, tmp_path, how, persist):
    target = expected_path(tmp_path)
    foreign = tmp_path / "foreign.bin"
    foreign.write_bytes(b"PARTIAL FOREIGN RECORD")
    stages, swaps = [], []
    real_staging_path, real_link = publication.staging_path, os.link

    def recording_staging_path(t):
        stage = real_staging_path(t)
        stages.append(stage)
        return stage

    def swapping_link(src, dst, *args, **kw):
        if persist == "always" or not swaps:
            swaps.append(True)
            stage = stages[-1]
            os.unlink(stage)
            if how == "hardlink":
                real_link(foreign, stage)
            else:
                stage.symlink_to(foreign)
        return real_link(src, dst, *args, **kw)

    monkeypatch.setattr(publication, "staging_path", recording_staging_path)
    monkeypatch.setattr(publication.os, "link", swapping_link)
    return target, foreign, swaps


@pytest.mark.parametrize("how", ["hardlink", "symlink"])
@pytest.mark.parametrize("persist", ["once", "always"])
@pytest.mark.parametrize("source", ["platform", "name"])
def test_a_staging_name_swapped_at_the_link_is_never_the_published_record(tmp_path, monkeypatch, how, persist, source):
    """Post-call state on both link sources: the verified record published, or
    a reported failure with the canonical path absent.

    On the descriptor source (Linux) nothing foreign is ever visible. On the
    name source the foreign file is visible from the link until the identity
    check takes the name down, and this test does not observe that interval:
    the name-fallback contract is the post-call state (publication module
    docstring).
    """
    if source == "name":
        monkeypatch.setattr(publication, "LINK_VIA_FD", False)
    target, foreign, swaps = _swap_stage_at_link(monkeypatch, tmp_path, how, persist)
    record = _record()
    if persist == "once":
        result = call(tmp_path)
        assert result.status == "published"
        assert target.read_bytes() == record
        assert os.lstat(target).st_ino != os.lstat(foreign).st_ino
        assert os.lstat(target).st_nlink == 1
        assert len(swaps) == 1
    else:
        with pytest.raises(wd.WriterError, match="consecutive attempts"):
            call(tmp_path)
        assert not os.path.lexists(target), "nothing foreign survives under the canonical name"
        assert len(swaps) == publication.PUBLISH_ATTEMPTS
    assert foreign.read_bytes() == b"PARTIAL FOREIGN RECORD", "the foreign file's own name is untouched"
    assert list(target.parent.glob(".stage.*")) == []


def test_the_published_record_is_the_only_name_for_its_inode(tmp_path):
    target = call(tmp_path).path
    assert os.lstat(target).st_nlink == 1, "a surviving second link would leave the immutable record writable elsewhere"


def _pin_staging_names(mp):
    """Make every staging unlink fail, leaving the stage in place."""
    real_unlink = os.unlink

    def failing_unlink(path):
        if os.path.basename(path).startswith(wd.STAGE_PREFIX):
            raise PermissionError("staging name pinned")
        real_unlink(path)

    mp.setattr(os, "unlink", failing_unlink)


def test_a_staging_name_that_cannot_be_released_is_a_reported_failure(tmp_path):
    # Staging cleanup is best-effort and never raises, so a failed unlink is
    # silent by design. It must not be silently *harmful*: a stage that
    # survives leaves the published inode reachable -- and writable -- under a
    # second name, which is the whole guarantee gone.
    with pytest.MonkeyPatch.context() as mp:
        _pin_staging_names(mp)
        with pytest.raises(wd.WriterError, match="still has 2 links"):
            call(tmp_path)


def test_a_retry_cannot_report_idempotent_while_a_writable_alias_survives(tmp_path):
    """The byte-identical retry row, closed.

    Finding the stored bytes already correct proves nothing about the *shape*
    of what is stored. A previous run can have landed the record and then
    failed to release its staging name, leaving a second name for the canonical
    inode -- writing through which rewrites the "immutable" record. So the
    retry re-proves the invariant instead of trusting the byte comparison, and
    reports failure for as long as the alias is still there.
    """
    with pytest.MonkeyPatch.context() as mp:
        _pin_staging_names(mp)
        with pytest.raises(wd.WriterError, match="still has 2 links"):
            call(tmp_path)

        target = expected_path(tmp_path)
        alias = next(iter(target.parent.glob(".stage.*")))
        assert os.lstat(alias).st_ino == os.lstat(target).st_ino, "the stage aliases the record"

        # Still failing, still refused: the bytes match, the shape does not.
        with pytest.raises(wd.WriterError, match="still has 2 links"):
            call(tmp_path)

    # Cleanup works again: the retry removes the alias and only then succeeds.
    result = call(tmp_path)
    assert result.status == "idempotent"
    assert os.lstat(result.path).st_nlink == 1
    assert list(result.path.parent.glob(".stage.*")) == []
    assert result.path.read_bytes() == _record()


def test_the_sweep_removes_an_alias_of_the_record_but_not_a_foreign_multi_link(tmp_path):
    # Both are multi-link staging files. One is the record itself under a
    # second name and must go; the other is somebody else's file that merely
    # matches the prefix, and is not this writer's to delete.
    with pytest.MonkeyPatch.context() as mp:
        _pin_staging_names(mp)
        with pytest.raises(wd.WriterError):
            call(tmp_path)

    target = expected_path(tmp_path)
    foreign = target.with_name(f"{wd.STAGE_PREFIX}{target.name}.foreign")
    foreign.write_bytes(b"someone else's partial")
    os.link(foreign, tmp_path / "their-own-name")

    assert call(tmp_path).status == "idempotent"
    survivors = sorted(p.name for p in target.parent.glob(".stage.*"))
    assert survivors == [foreign.name]
    assert os.lstat(target).st_nlink == 1


def test_a_symlink_at_the_canonical_path_is_refused_without_being_followed(tmp_path):
    target = expected_path(tmp_path)
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"untouched\n")
    target.symlink_to(outside)

    with pytest.raises(wd.WriterError, match="symlink"):
        call(tmp_path)
    assert outside.read_bytes() == b"untouched\n"


# ---- CONFORMANCE 3: read-back mismatch -----------------------------------


def test_read_back_mismatch_is_a_reported_failure(tmp_path, monkeypatch):
    real_link = os.link

    def tampering_link(src, dst, *args, **kw):
        # Publication lands the staged inode, but storage hands back different
        # bytes than were verified. The inode binding cannot see this; only the
        # read-back can.
        real_link(src, dst, *args, **kw)
        # dst is a name relative to a directory descriptor where the link is
        # descriptor-bound, so tamper with the record through its known path.
        with open(expected_path(tmp_path), "r+b") as fh:
            fh.seek(-1, os.SEEK_END)
            fh.write(b"X")

    monkeypatch.setattr(os, "link", tampering_link)
    with pytest.raises(wd.WriterError, match="read-back mismatch"):
        call(tmp_path)


def test_a_leading_dot_project_name_is_rejected(tmp_path):
    for project in ("..", ".inf", ".hidden"):
        with pytest.raises(wd.WriterError) as exc:
            call(tmp_path, project=project)
        assert exc.value.code == 1
    assert not (tmp_path / "org-memory").exists()


# ---- CONFORMANCE 4: retry after success (idempotent) ----------------------


def test_retry_after_success_is_idempotent(tmp_path):
    first_target, first_status, first_hash, _ = call(tmp_path)
    assert first_status == "published"
    stamp = first_target.stat().st_mtime_ns

    second_target, second_status, second_hash, _ = call(tmp_path)
    assert second_status == "idempotent"
    assert second_target == first_target
    assert second_hash == first_hash
    assert second_target.stat().st_mtime_ns == stamp, "record must not be rewritten"
    assert list(first_target.parent.glob(".stage.*")) == []


# ---- CONFORMANCE 5: differing-content collision ---------------------------


def test_differing_content_collision_is_a_hard_failure(tmp_path):
    target, _, _, _ = call(tmp_path)
    original = target.read_bytes()

    with pytest.raises(wd.WriterError, match="new session identifier"):
        call(tmp_path, body=b"# Session\n\nDifferent content.\n")

    assert target.read_bytes() == original, "a published debrief is never replaced"
    assert list(target.parent.glob(".stage.*")) == []


def test_collision_recovery_is_a_new_session_identifier(tmp_path):
    first, _, _, _ = call(tmp_path)
    second, status, _, _ = call(tmp_path, body=b"# Session\n\nDifferent.\n", session="1f3a9c2c")
    assert status == "published"
    assert first.is_file() and second.is_file()
    assert first != second


# ---- CONFORMANCE 6: symlink at target ------------------------------------


def test_symlink_at_target_is_refused(tmp_path):
    target = expected_path(tmp_path)
    target.parent.mkdir(parents=True)
    decoy = tmp_path / "elsewhere.md"
    decoy.write_bytes(b"not a debrief\n")
    target.symlink_to(decoy)

    with pytest.raises(wd.WriterError, match="symlink"):
        call(tmp_path)

    assert decoy.read_bytes() == b"not a debrief\n", "writer must not write through the link"
    assert target.is_symlink(), "the hostile target is left as found, not silently replaced"


def test_directory_at_target_is_refused(tmp_path):
    target = expected_path(tmp_path)
    target.mkdir(parents=True)
    with pytest.raises(wd.WriterError, match="not a regular file"):
        call(tmp_path)


# ---- CONFORMANCE 7: concurrent identical and differing writers ------------


def _run_concurrently(fns):
    results, errors = [], []
    barrier = threading.Barrier(len(fns))

    def runner(fn):
        try:
            barrier.wait()
            results.append(fn())
        except Exception as exc:  # noqa: BLE001 - recorded for assertions
            errors.append(exc)

    threads = [threading.Thread(target=runner, args=(fn,)) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def test_concurrent_identical_writers_both_succeed(tmp_path):
    results, errors = _run_concurrently([lambda: call(tmp_path)] * 4)
    assert errors == [], f"identical concurrent writers must not fail: {errors}"
    statuses = sorted(r.status for r in results)
    assert statuses.count("published") == 1, "exactly one writer publishes"
    assert statuses.count("idempotent") == 3, "the rest resolve as idempotent"

    target = expected_path(tmp_path)
    assert target.is_file()
    assert list(target.parent.glob(".stage.*")) == []


def test_concurrent_differing_writers_leave_one_intact_record(tmp_path):
    bodies = [b"# A\n", b"# B\n", b"# C\n"]
    results, errors = _run_concurrently([(lambda b=b: call(tmp_path, body=b)) for b in bodies])
    assert len(results) == 1, "exactly one differing writer may publish"
    assert len(errors) == len(bodies) - 1
    assert all(isinstance(e, wd.WriterError) for e in errors)

    target = expected_path(tmp_path)
    _, stored = wd.split_record(target.read_bytes())
    assert stored in bodies, "the landed record is one writer's complete body"
    assert list(target.parent.glob(".stage.*")) == []


# ---- the vanished stage, pinned deterministically ---------------------------
# CI 2026-09-06 (Linux, four identical writers): a concurrent writer's
# post-publish sweep unlinked another writer's freshly created stage, and the
# fstat after the O_EXCL create reported 0 links, which the script's code read
# as a hard error. The three shapes below are the ones the writer must resolve.


def _sweep_during_staging(monkeypatch, land):
    """Make the first fsync of a staging file act as a concurrent writer's sweep.

    The staging name is unlinked between this writer's O_EXCL create and its
    fstat; with ``land`` given, that record is published at the canonical path
    first, the way a real sweep is always preceded by a publication.
    """
    stages = []
    real_staging_path, real_fsync = publication.staging_path, os.fsync
    fired = []

    def recording_staging_path(target):
        stage = real_staging_path(target)
        stages.append((stage, target))
        return stage

    def sweeping_fsync(fd):
        real_fsync(fd)
        if not fired:
            fired.append(True)
            stage, target = stages[-1]
            if land is not None:
                target.write_bytes(land)
            os.unlink(stage)

    monkeypatch.setattr(publication, "staging_path", recording_staging_path)
    monkeypatch.setattr(publication.os, "fsync", sweeping_fsync)
    return fired


def test_a_stage_swept_before_its_fstat_resolves_as_idempotent(tmp_path, monkeypatch):
    record = call(tmp_path, dry_run=True).record
    fired = _sweep_during_staging(monkeypatch, land=record)
    result = call(tmp_path)
    assert fired
    assert result.status == "idempotent"
    assert expected_path(tmp_path).read_bytes() == record
    assert list(expected_path(tmp_path).parent.glob(".stage.*")) == []


def test_a_stage_swept_before_its_fstat_under_a_different_record_is_a_collision(tmp_path, monkeypatch):
    other = b"# another writer's record\n"
    fired = _sweep_during_staging(monkeypatch, land=other)
    with pytest.raises(wd.WriterError, match="different record"):
        call(tmp_path)
    assert fired
    assert expected_path(tmp_path).read_bytes() == other, "the landed record is never replaced"
    assert list(expected_path(tmp_path).parent.glob(".stage.*")) == []


def test_a_stage_swept_before_its_fstat_with_nothing_landed_is_retried(tmp_path, monkeypatch):
    fired = _sweep_during_staging(monkeypatch, land=None)
    result = call(tmp_path)
    assert fired
    assert result.status == "published"
    assert expected_path(tmp_path).read_bytes() == call(tmp_path, dry_run=True).record
    assert list(expected_path(tmp_path).parent.glob(".stage.*")) == []


# ------------------------------------------------------------------- CLI ---


def test_cli_publishes_and_reports_json(tmp_path, capsys):
    body_file = tmp_path / "body.md"
    body_file.write_bytes(BODY)
    assert main(cli_args(tmp_path, body_file, "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert list(payload) == ["path", "status", "content_sha256", "schema_version"]
    assert payload["status"] == "published"
    assert payload["content_sha256"] == wd.content_sha256(BODY)
    assert payload["schema_version"] == 1
    assert Path(payload["path"]) == expected_path(tmp_path)


def test_cli_text_output_names_status_path_and_hash(tmp_path, capsys):
    body_file = tmp_path / "body.md"
    body_file.write_bytes(BODY)
    assert main(cli_args(tmp_path, body_file)) == 0
    out = capsys.readouterr().out
    assert out == f"published: {expected_path(tmp_path)}\ncontent_sha256: {wd.content_sha256(BODY)}\n"


def test_cli_returns_one_on_validation_error(tmp_path, capsys):
    body_file = tmp_path / "body.md"
    body_file.write_bytes(BODY)
    args = cli_args(tmp_path, body_file)
    args[args.index("1f3a9c2b")] = "not-valid"
    assert main(args) == 1
    assert "session" in capsys.readouterr().err


def test_cli_returns_one_on_an_unreadable_body(tmp_path, capsys):
    assert main(cli_args(tmp_path, tmp_path / "missing.md")) == 1
    assert "cannot read body" in capsys.readouterr().err
    assert not (tmp_path / "org-memory").exists()


def test_cli_returns_two_on_publication_failure(tmp_path, capsys):
    body_file = tmp_path / "body.md"
    body_file.write_bytes(BODY)
    assert main(cli_args(tmp_path, body_file)) == 0
    body_file.write_bytes(b"# Different\n")
    assert main(cli_args(tmp_path, body_file)) == 2
    assert "new session identifier" in capsys.readouterr().err


def test_cli_reads_the_body_from_stdin(tmp_path, capsys, monkeypatch):
    import io

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(BODY)))
    assert main(cli_args(tmp_path, Path("-"), "--json")) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "published"
    assert expected_path(tmp_path).read_bytes() == GOLDEN.read_bytes()


def test_cli_accepts_the_hidden_oacp_dir_alias(tmp_path, capsys):
    body_file = tmp_path / "body.md"
    body_file.write_bytes(BODY)
    store = tmp_path / "store"
    args = cli_args(store, body_file, "--json")
    args[args.index("--home")] = "--oacp-dir"
    assert main(args) == 0
    assert Path(json.loads(capsys.readouterr().out)["path"]) == expected_path(store)
    with pytest.raises(SystemExit) as exit_info:
        main(["debrief", "write", "--help"])
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--home" in help_text and "--oacp-dir" not in help_text


def test_cli_resolves_the_home_like_every_other_verb(tmp_path, capsys, monkeypatch):
    body_file = tmp_path / "body.md"
    body_file.write_bytes(BODY)
    store = tmp_path / "env-store"
    monkeypatch.setenv(ENV_HOME, str(store))
    monkeypatch.delenv(ENV_COMPAT_HOME, raising=False)
    args = cli_args(store, body_file, "--json")
    del args[args.index("--home") : args.index("--home") + 2]
    assert main(args) == 0
    assert Path(json.loads(capsys.readouterr().out)["path"]) == expected_path(store)


def test_debrief_without_a_subcommand_shows_help(capsys):
    assert main(["debrief"]) == 2
    assert "usage: agent-memory" in capsys.readouterr().err


# ------------------------------------------------- frontmatter serialization ---

# Every one of these is a legal value under the protocol's identity grammars,
# and every one is a YAML plain-scalar keyword or indicator: emitted unquoted,
# a real parser reads them back as a bool, None, an int, or a syntax error --
# silently changing the record's identity, including `immutable`.
YAML_HOSTILE_IDENTIFIERS = [
    "true", "True", "TRUE", "false", "False", "null", "Null", "NULL",
    "yes", "no", "on", "off", "y", "n", "0x1f", "0o7", "1e3", "12", "1.0",
]


@pytest.mark.parametrize("value", YAML_HOSTILE_IDENTIFIERS)
def test_identity_values_survive_a_real_yaml_parser(tmp_path, value):
    yaml = pytest.importorskip("yaml")
    target = call(tmp_path, agent=value, session="1f3a9c2b").path
    head = target.read_bytes().split(b"---\n")[1].decode("utf-8")
    parsed = yaml.safe_load(head)
    assert parsed["agent"] == value, f"{value!r} was re-typed to {parsed['agent']!r}"
    assert isinstance(parsed["agent"], str)
    assert parsed["immutable"] is True
    assert parsed["schema_version"] == 1
    assert isinstance(parsed["content_sha256"], str)


@pytest.mark.parametrize("field", ["project", "agent", "runtime", "session"])
def test_every_identity_field_is_quoted(tmp_path, field):
    yaml = pytest.importorskip("yaml")
    value = {"project": "true", "agent": "null", "runtime": "no", "session": "on"}[field]
    target = call(tmp_path, **{field: value}).path
    parsed = yaml.safe_load(target.read_bytes().split(b"---\n")[1].decode("utf-8"))
    assert parsed[field] == value and isinstance(parsed[field], str)


@pytest.mark.parametrize(
    "project",
    ["*alias", "&anchor", "- item", "? key", "{a}", "[a]", "| block", "> fold",
     "%directive", "@reserved", "`tick", "a: b", "a #c", "'quoted'", '"dquoted"',
     "-1", "_", "~", "1.0", "0x1f"],
)
def test_yaml_indicator_project_names_stay_readable_strings(tmp_path, project):
    yaml = pytest.importorskip("yaml")
    target = call(tmp_path, project=project).path
    parsed = yaml.safe_load(target.read_bytes().split(b"---\n")[1].decode("utf-8"))
    assert parsed["project"] == project


@pytest.mark.parametrize("value", YAML_HOSTILE_IDENTIFIERS + ["*alias", "a: b", "'quoted'", "it's"])
def test_identity_values_survive_the_writers_own_reader(tmp_path, value):
    # The parser-independent half of the serialization pin: the writer's own
    # split_record, which the read-back and the content hash are defined over,
    # returns every hostile identifier as the string it was given.
    target = call(tmp_path, project=value).path
    fm, _ = wd.split_record(target.read_bytes())
    assert fm["project"] == value and fm["immutable"] == "true" and fm["schema_version"] == "1"


def test_composition_refuses_a_record_whose_frontmatter_does_not_round_trip(tmp_path, monkeypatch):
    # The last line of defense: if the serializer ever regresses, composition
    # fails loudly instead of publishing a record that says something else.
    # Nothing downstream can catch this -- the read-back compares the stored
    # file against these same composed bytes, and the doctor never opens
    # debrief files.
    monkeypatch.setattr(wd, "_yaml_scalar", str)
    with pytest.raises(wd.WriterError, match="did not round-trip"):
        call(tmp_path, agent="true")
    assert not expected_path(tmp_path).exists()


@pytest.mark.parametrize("value", ["a", "A", "0", "z" * 64, "a.b", "a_b", "a-b", "A.0_z-Q", "true", "null"])
def test_agent_grammar_boundaries_are_accepted_and_parseable(tmp_path, value):
    result = call(tmp_path, agent=value)
    assert result.path.name == f"20260825-{value}-1f3a9c2b.md"
    stem = result.path.stem
    assert stem.rsplit("-", 1)[1] == "1f3a9c2b", "session stays parseable after the final hyphen"


@pytest.mark.parametrize("value", ["", "z" * 65, ".hidden", "-lead", "a/b", "a b", "a\tb"])
def test_agent_grammar_violations_are_rejected(tmp_path, value):
    with pytest.raises(wd.WriterError) as exc:
        call(tmp_path, agent=value)
    assert exc.value.code == 1


@pytest.mark.parametrize("value", ["", "z" * 33, "Abc", "a-b", "a_b", "a.b"])
def test_session_grammar_violations_are_rejected(tmp_path, value):
    with pytest.raises(wd.WriterError) as exc:
        call(tmp_path, session=value)
    assert exc.value.code == 1


# ------------------------------------------------------------ body encoding ---


@pytest.mark.parametrize("body", [b"\xff\xfe", b"# ok\n\xc3\x28\n", b"\xed\xa0\x80", "ok\n".encode("utf-16")])
def test_a_body_that_is_not_utf8_is_rejected_before_the_store_is_touched(tmp_path, body):
    with pytest.raises(wd.WriterError, match="not valid UTF-8") as exc:
        call(tmp_path, body=body)
    assert exc.value.code == 1
    assert not (tmp_path / "org-memory").exists(), "no directories are created"


def test_multibyte_utf8_bodies_round_trip_byte_for_byte(tmp_path):
    body = "# Sesión\n\n— ✅ 完了\n".encode("utf-8")
    result = call(tmp_path, body=body)
    _, stored = wd.split_record(result.path.read_bytes())
    assert stored == body
    assert result.content_sha256 == wd.content_sha256(body)


# ----------------------------------------------------------------- dry run ---


def test_dry_run_composes_the_real_record_and_writes_nothing(tmp_path):
    result = call(tmp_path, dry_run=True)
    assert result.status == "dry-run"
    assert result.path == expected_path(tmp_path)
    assert result.record == _record()
    assert not (tmp_path / "org-memory").exists(), "the store is not even created"


def test_dry_run_still_validates(tmp_path):
    with pytest.raises(wd.WriterError) as exc:
        call(tmp_path, session="not-valid", dry_run=True)
    assert exc.value.code == 1


def test_dry_run_then_publish_lands_the_previewed_bytes(tmp_path):
    previewed = call(tmp_path, dry_run=True).record
    published = call(tmp_path)
    assert published.status == "published"
    assert published.path.read_bytes() == previewed


def test_cli_dry_run_writes_nothing_and_reports_the_status(tmp_path, capsys):
    body_file = tmp_path / "body.md"
    body_file.write_bytes(BODY)
    assert main(cli_args(tmp_path, body_file, "--dry-run", "--json")) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "dry-run"
    assert BODY.decode() in captured.err, "the composed record is shown for review"
    assert not (tmp_path / "org-memory").exists()


# ------------------------------------------------------------ installed verb ---


def test_the_verb_runs_from_an_unrelated_working_directory(tmp_path):
    """The console script, from a working directory unrelated to the home, with no kernel and no env var."""
    project = tmp_path / "elsewhere"
    project.mkdir()
    body_file = project / "body.md"
    body_file.write_bytes(BODY)
    store = tmp_path / "store"
    env = {key: value for key, value in os.environ.items() if key not in (ENV_HOME, ENV_COMPAT_HOME)}
    args = [sys.executable, "-m", "agent_memory", *cli_args(store, body_file, "--json")]

    dry = subprocess.run(args + ["--dry-run"], cwd=project, capture_output=True, text=True, env=env)
    assert dry.returncode == 0, dry.stderr
    assert '"status": "dry-run"' in dry.stdout
    assert not (store / "org-memory").exists(), "dry run wrote nothing"

    live = subprocess.run(args, cwd=project, capture_output=True, text=True, env=env)
    assert live.returncode == 0, live.stderr
    assert '"status": "published"' in live.stdout
    assert expected_path(store).read_bytes() == GOLDEN.read_bytes()
