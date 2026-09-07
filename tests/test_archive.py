from __future__ import annotations

import datetime as dt
import errno
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

import pytest

from agent_memory import archive, layout
from agent_memory.cli import main

FIXED_NOW = dt.datetime(2026, 3, 20, 1, 2, 3, tzinfo=dt.timezone.utc)
ARCHIVED = "20260320T010203Z_notes.md"


@pytest.fixture
def project_root(tmp_path: Path) -> Tuple[Path, Path]:
    """A home with one project whose memory and archive directories exist; returns (home, project dir)."""
    home = tmp_path / "home"
    project = home / "projects" / "demo"
    (project / "memory" / "archive").mkdir(parents=True)
    return home, project


def _stat(path: Path) -> Tuple[int, int, int]:
    st = path.stat()
    return st.st_mode, st.st_size, st.st_mtime_ns


# --- the ported behavior ---------------------------------------------------


def test_archive_moves_a_supplementary_file_into_the_archive(project_root: Tuple[Path, Path]) -> None:
    home, project = project_root
    source = project / "memory" / "notes.md"
    source.write_text("# notes\n", encoding="utf-8")

    result = archive.archive(home, "demo", "notes.md", now=FIXED_NOW)

    destination = project / "memory" / "archive" / result["archived_file"]
    assert result["status"] == "archived"
    assert result["archived_file"] == ARCHIVED
    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "# notes\n"
    assert result["source"] == str(source) and result["destination"] == str(destination)


def test_restore_moves_an_archived_file_back(project_root: Tuple[Path, Path]) -> None:
    home, project = project_root
    archived = project / "memory" / "archive" / ARCHIVED
    archived.write_text("# archived\n", encoding="utf-8")

    result = archive.restore(home, "demo", ARCHIVED)

    destination = project / "memory" / result["restored_file"]
    assert result["status"] == "restored"
    assert result["restored_file"] == "notes.md"
    assert not archived.exists()
    assert destination.read_text(encoding="utf-8") == "# archived\n"


def test_archive_rejects_a_missing_source(project_root: Tuple[Path, Path]) -> None:
    home, _ = project_root
    with pytest.raises(archive.ArchiveError, match="memory file not found"):
        archive.archive(home, "demo", "notes.md")


def test_archive_rejects_an_existing_destination(project_root: Tuple[Path, Path]) -> None:
    home, project = project_root
    (project / "memory" / "notes.md").write_text("# notes\n", encoding="utf-8")
    (project / "memory" / "archive" / ARCHIVED).write_text("# existing\n", encoding="utf-8")
    with pytest.raises(archive.ArchiveError, match="archive destination already exists"):
        archive.archive(home, "demo", "notes.md", now=FIXED_NOW)
    assert (project / "memory" / "archive" / ARCHIVED).read_text(encoding="utf-8") == "# existing\n"
    assert (project / "memory" / "notes.md").is_file()


def test_restore_rejects_an_existing_active_destination(project_root: Tuple[Path, Path]) -> None:
    home, project = project_root
    (project / "memory" / "archive" / ARCHIVED).write_text("# archived\n", encoding="utf-8")
    (project / "memory" / "notes.md").write_text("# current\n", encoding="utf-8")
    with pytest.raises(archive.ArchiveError, match="active memory destination already exists"):
        archive.restore(home, "demo", ARCHIVED)
    assert (project / "memory" / "notes.md").read_text(encoding="utf-8") == "# current\n"


def test_restore_rejects_a_missing_archive_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "projects" / "demo" / "memory").mkdir(parents=True)
    with pytest.raises(archive.ArchiveError, match="memory archive directory not found"):
        archive.restore(home, "demo", ARCHIVED)


@pytest.mark.parametrize("bad", ["../notes.md", "a/b.md", "a\\b.md", ".hidden", "", "-dash", "x" * 129, "sp ace.md"])
def test_archive_rejects_names_that_are_not_simple_basenames(project_root: Tuple[Path, Path], bad: str) -> None:
    home, _ = project_root
    with pytest.raises(archive.ArchiveError, match="simple basename"):
        archive.archive(home, "demo", bad)


@pytest.mark.parametrize("bad", ["../demo", "a/b", ".hidden", ""])
def test_archive_rejects_project_names_with_separators_or_leading_dots(
    project_root: Tuple[Path, Path], bad: str
) -> None:
    home, _ = project_root
    with pytest.raises(archive.ArchiveError, match="project name must"):
        archive.archive(home, bad, "notes.md")


@pytest.mark.parametrize("bad", ["notes.md", "2026_notes.md", "20260320T010203_notes.md", "20260320T010203Z_", "../x"])
def test_restore_rejects_archived_names_off_the_grammar(project_root: Tuple[Path, Path], bad: str) -> None:
    home, _ = project_root
    with pytest.raises(archive.ArchiveError, match="simple basename|must match <UTC timestamp>_<basename>"):
        archive.restore(home, "demo", bad)


@pytest.mark.parametrize("protected", layout.PROJECT.files)
def test_archive_refuses_the_active_names(project_root: Tuple[Path, Path], protected: str) -> None:
    home, project = project_root
    (project / "memory" / protected).write_text("# active\n", encoding="utf-8")
    with pytest.raises(archive.ArchiveError, match="cannot archive standard active memory file"):
        archive.archive(home, "demo", protected)
    with pytest.raises(archive.ArchiveError, match="cannot archive standard active memory file"):
        archive.archive(home, "demo", protected, dry_run=True)
    assert (project / "memory" / protected).is_file()


def test_the_protected_names_are_the_layout_table(project_root: Tuple[Path, Path]) -> None:
    assert archive.PROTECTED_FILES == ("project_facts.md", "decision_log.md", "open_threads.md", "known_debt.md")
    home, project = project_root
    (project / "memory" / "archive" / "20260101T000000Z_open_threads.md").write_text("old\n", encoding="utf-8")
    result = archive.restore(home, "demo", "20260101T000000Z_open_threads.md")
    assert result["restored_file"] == "open_threads.md"
    assert (project / "memory" / "open_threads.md").read_text(encoding="utf-8") == "old\n"


# --- dry-run parity --------------------------------------------------------


def test_archive_dry_run_reports_the_same_paths_and_changes_nothing(project_root: Tuple[Path, Path]) -> None:
    home, project = project_root
    source = project / "memory" / "notes.md"
    source.write_text("# notes\n", encoding="utf-8")

    dry = archive.archive(home, "demo", "notes.md", dry_run=True, now=FIXED_NOW)
    assert dry["status"] == "dry-run" and dry["dry_run"] is True
    assert source.is_file()
    assert not (project / "memory" / "archive" / ARCHIVED).exists()

    real = archive.archive(home, "demo", "notes.md", now=FIXED_NOW)
    assert {k: v for k, v in dry.items() if k not in ("status", "dry_run")} == {
        k: v for k, v in real.items() if k not in ("status", "dry_run")
    }


def test_restore_dry_run_reports_the_same_paths_and_changes_nothing(project_root: Tuple[Path, Path]) -> None:
    home, project = project_root
    archived = project / "memory" / "archive" / ARCHIVED
    archived.write_text("# archived\n", encoding="utf-8")

    dry = archive.restore(home, "demo", ARCHIVED, dry_run=True)
    assert dry["status"] == "dry-run"
    assert archived.is_file() and not (project / "memory" / "notes.md").exists()

    real = archive.restore(home, "demo", ARCHIVED)
    assert {k: v for k, v in dry.items() if k not in ("status", "dry_run")} == {
        k: v for k, v in real.items() if k not in ("status", "dry_run")
    }


def test_dry_run_fails_on_everything_the_real_run_fails_on(project_root: Tuple[Path, Path]) -> None:
    home, project = project_root
    with pytest.raises(archive.ArchiveError, match="memory file not found"):
        archive.archive(home, "demo", "notes.md", dry_run=True)
    (project / "memory" / "notes.md").write_text("# notes\n", encoding="utf-8")
    (project / "memory" / "archive" / ARCHIVED).write_text("# existing\n", encoding="utf-8")
    with pytest.raises(archive.ArchiveError, match="archive destination already exists"):
        archive.archive(home, "demo", "notes.md", dry_run=True, now=FIXED_NOW)
    with pytest.raises(archive.ArchiveError, match="active memory destination already exists"):
        archive.restore(home, "demo", ARCHIVED, dry_run=True)


# --- no replace: the interleaving probes ------------------------------------


def _interleave(monkeypatch: pytest.MonkeyPatch, plant: Callable[[], None]) -> None:
    """Run ``plant`` at the instant of the move, after every check has passed and before the real link."""
    real_link = os.link

    def link(src: Any, dst: Any, **kwargs: Any) -> None:
        plant()
        real_link(src, dst, **kwargs)

    monkeypatch.setattr(archive.os, "link", link)


def test_archive_fails_closed_when_the_destination_appears_after_the_check(
    project_root: Tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, project = project_root
    source = project / "memory" / "notes.md"
    source.write_text("source\n", encoding="utf-8")
    destination = project / "memory" / "archive" / ARCHIVED
    _interleave(monkeypatch, lambda: destination.write_text("concurrent destination\n", encoding="utf-8"))

    with pytest.raises(archive.ArchiveError, match="destination already exists"):
        archive.archive(home, "demo", "notes.md", now=FIXED_NOW)

    assert source.read_text(encoding="utf-8") == "source\n"
    assert (project / "memory" / "archive" / ARCHIVED).read_text(encoding="utf-8") == "concurrent destination\n"


def test_restore_fails_closed_when_the_destination_appears_after_the_check(
    project_root: Tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, project = project_root
    archived = project / "memory" / "archive" / ARCHIVED
    archived.write_text("source\n", encoding="utf-8")
    destination = project / "memory" / "notes.md"
    _interleave(monkeypatch, lambda: destination.write_text("concurrent destination\n", encoding="utf-8"))

    with pytest.raises(archive.ArchiveError, match="destination already exists"):
        archive.restore(home, "demo", ARCHIVED)

    assert archived.read_text(encoding="utf-8") == "source\n"
    assert (project / "memory" / "notes.md").read_text(encoding="utf-8") == "concurrent destination\n"


def test_archive_fails_closed_when_a_symlink_appears_at_the_destination(
    project_root: Tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, project = project_root
    source = project / "memory" / "notes.md"
    source.write_text("source\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("elsewhere\n", encoding="utf-8")
    _interleave(monkeypatch, lambda: (project / "memory" / "archive" / ARCHIVED).symlink_to(elsewhere))

    with pytest.raises(archive.ArchiveError, match="destination already exists"):
        archive.archive(home, "demo", "notes.md", now=FIXED_NOW)

    assert source.read_text(encoding="utf-8") == "source\n"
    assert elsewhere.read_text(encoding="utf-8") == "elsewhere\n"


# --- containment: the symlink probes -----------------------------------------


def test_archive_refuses_a_symlinked_memory_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside-memory"
    outside.mkdir()
    (outside / "elsewhere.md").write_text("outside target\n", encoding="utf-8")
    home = tmp_path / "home"
    project = home / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "memory").symlink_to(outside, target_is_directory=True)

    with pytest.raises(archive.ArchiveError, match="symlink"):
        archive.archive(home, "demo", "elsewhere.md", now=FIXED_NOW)

    assert (outside / "elsewhere.md").read_text(encoding="utf-8") == "outside target\n"
    assert not (outside / "archive").exists()


@pytest.mark.parametrize("link", ["projects", "projects/demo", "projects/demo/memory", "projects/demo/memory/archive"])
def test_every_directory_below_the_home_must_be_real(tmp_path: Path, link: str) -> None:
    real_home = tmp_path / "real-home"
    real_project = real_home / "projects" / "demo"
    (real_project / "memory" / "archive").mkdir(parents=True)
    (real_project / "memory" / "notes.md").write_text("# notes\n", encoding="utf-8")
    (real_project / "memory" / "archive" / ARCHIVED).write_text("# archived\n", encoding="utf-8")
    home = tmp_path / "home"
    target = real_home / link
    linked = home / link
    linked.parent.mkdir(parents=True, exist_ok=True)
    linked.symlink_to(target, target_is_directory=True)
    for part in Path(link).parents:
        if part != Path(".") and not (real_home / part).exists():
            (real_home / part).mkdir(parents=True)

    with pytest.raises(archive.ArchiveError, match="symlink"):
        archive.archive(home, "demo", "notes.md", now=FIXED_NOW)
    with pytest.raises(archive.ArchiveError, match="symlink"):
        archive.restore(home, "demo", ARCHIVED)
    with pytest.raises(archive.ArchiveError, match="symlink"):
        archive.archive(home, "demo", "notes.md", dry_run=True, now=FIXED_NOW)

    assert (real_project / "memory" / "notes.md").is_file()
    assert (real_project / "memory" / "archive" / ARCHIVED).is_file()


def test_archive_and_restore_refuse_a_symlinked_file(project_root: Tuple[Path, Path], tmp_path: Path) -> None:
    home, project = project_root
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("elsewhere\n", encoding="utf-8")
    (project / "memory" / "notes.md").symlink_to(elsewhere)
    (project / "memory" / "archive" / ARCHIVED).symlink_to(elsewhere)

    with pytest.raises(archive.ArchiveError, match="refusing to move a symlink"):
        archive.archive(home, "demo", "notes.md", now=FIXED_NOW)
    with pytest.raises(archive.ArchiveError, match="refusing to move a symlink"):
        archive.restore(home, "demo", ARCHIVED)

    assert elsewhere.read_text(encoding="utf-8") == "elsewhere\n"
    assert (project / "memory" / "notes.md").is_symlink()
    assert (project / "memory" / "archive" / ARCHIVED).is_symlink()


def test_a_symlinked_home_itself_is_fine(tmp_path: Path) -> None:
    real_home = tmp_path / "real-home"
    project = real_home / "projects" / "demo"
    (project / "memory" / "archive").mkdir(parents=True)
    (project / "memory" / "notes.md").write_text("# notes\n", encoding="utf-8")
    home = tmp_path / "home"
    home.symlink_to(real_home, target_is_directory=True)

    result = archive.archive(home, "demo", "notes.md", now=FIXED_NOW)

    assert result["status"] == "archived"
    assert (project / "memory" / "archive" / ARCHIVED).is_file()


# --- the move itself ---------------------------------------------------------


def test_bytes_and_metadata_survive_archive_and_restore(project_root: Tuple[Path, Path]) -> None:
    home, project = project_root
    source = project / "memory" / "notes.md"
    payload = bytes(range(256)) * 3
    source.write_bytes(payload)
    source.chmod(0o640)
    os.utime(source, ns=(1_600_000_000_000_000_000, 1_500_000_000_123_456_789))
    before = _stat(source)

    archived = project / "memory" / "archive" / archive.archive(home, "demo", "notes.md", now=FIXED_NOW)["archived_file"]
    assert archived.read_bytes() == payload
    assert _stat(archived) == before
    assert not source.exists()

    archive.restore(home, "demo", archived.name)
    assert source.read_bytes() == payload
    assert _stat(source) == before
    assert not archived.exists()


def test_archive_creates_the_archive_directory_when_missing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    memory = home / "projects" / "demo" / "memory"
    memory.mkdir(parents=True)
    (memory / "notes.md").write_text("# notes\n", encoding="utf-8")
    dry = archive.archive(home, "demo", "notes.md", dry_run=True, now=FIXED_NOW)
    assert not (memory / "archive").exists()
    result = archive.archive(home, "demo", "notes.md", now=FIXED_NOW)
    assert (memory / "archive" / ARCHIVED).is_file()
    assert result["destination"] == dry["destination"]


def test_a_filesystem_without_hard_links_is_refused(
    project_root: Tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, project = project_root
    source = project / "memory" / "notes.md"
    source.write_text("# notes\n", encoding="utf-8")

    def no_links(src: Any, dst: Any, **kwargs: Any) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(archive.os, "link", no_links)
    with pytest.raises(archive.ArchiveError, match="hard links"):
        archive.archive(home, "demo", "notes.md", now=FIXED_NOW)
    assert source.is_file()
    assert not (project / "memory" / "archive" / ARCHIVED).exists()


def test_a_source_that_cannot_be_removed_is_reported_not_hidden(
    project_root: Tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, project = project_root
    source = project / "memory" / "notes.md"
    source.write_text("# notes\n", encoding="utf-8")

    def no_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(archive.os, "unlink", no_unlink)
    with pytest.raises(archive.ArchiveError, match="source could not be removed"):
        archive.archive(home, "demo", "notes.md", now=FIXED_NOW)
    assert source.is_file()
    assert (project / "memory" / "archive" / ARCHIVED).is_file()


def test_non_posix_platforms_are_refused(project_root: Tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    home, project = project_root
    (project / "memory" / "notes.md").write_text("# notes\n", encoding="utf-8")
    monkeypatch.setattr(archive, "_PLATFORM", "nt")
    for dry_run in (False, True):
        with pytest.raises(archive.ArchiveError, match="POSIX"):
            archive.archive(home, "demo", "notes.md", dry_run=dry_run, now=FIXED_NOW)
        with pytest.raises(archive.ArchiveError, match="POSIX"):
            archive.restore(home, "demo", ARCHIVED, dry_run=dry_run)
    assert (project / "memory" / "notes.md").is_file()


# --- the command line -------------------------------------------------------


def test_archive_verb_json_output(project_root: Tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    home, project = project_root
    (project / "memory" / "notes.md").write_text("# notes\n", encoding="utf-8")
    assert main(["archive", "demo", "notes.md", "--home", str(home), "--json"]) == 0
    payload: Dict[str, Any] = json.loads(capsys.readouterr().out)
    assert payload["action"] == "archive" and payload["status"] == "archived"
    assert Path(payload["destination"]).is_file()
    assert payload["archived_file"].endswith("_notes.md")


def test_restore_verb_json_output(project_root: Tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    home, project = project_root
    (project / "memory" / "archive" / ARCHIVED).write_text("# archived\n", encoding="utf-8")
    assert main(["restore", "demo", ARCHIVED, "--home", str(home), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "restore" and payload["restored_file"] == "notes.md"


def test_archive_and_restore_verbs_human_output(
    project_root: Tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    home, project = project_root
    (project / "memory" / "notes.md").write_text("# notes\n", encoding="utf-8")
    assert main(["archive", "demo", "notes.md", "--home", str(home), "--dry-run"]) == 0
    assert capsys.readouterr().out.startswith("Would archive memory/notes.md -> memory/archive/")
    assert main(["archive", "demo", "notes.md", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Archived memory/notes.md -> memory/archive/")
    archived = out.split("memory/archive/")[1].strip()
    assert main(["restore", "demo", archived, "--home", str(home)]) == 0
    assert capsys.readouterr().out == f"Restored memory/archive/{archived} -> memory/notes.md\n"


def test_archive_verb_refusals_exit_nonzero(project_root: Tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    home, _ = project_root
    assert main(["archive", "demo", "known_debt.md", "--home", str(home)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "agent-memory: error: cannot archive standard active memory file" in captured.err
    assert main(["restore", "demo", "notes.md", "--home", str(home)]) == 1
    assert "must match <UTC timestamp>_<basename>" in capsys.readouterr().err


# --- containment at the syscall boundary: a parent swapped after validation --


_PARENT_LEVELS = ("projects", "projects/demo", "projects/demo/memory", "projects/demo/memory/archive")


def _tree(root: Path) -> Dict[str, Any]:
    """Every entry below ``root`` with its kind and content, for a before/after comparison."""
    entries: Dict[str, Any] = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        if path.is_symlink():
            entries[key] = ("symlink", os.readlink(path))
        elif path.is_dir():
            entries[key] = ("dir", None)
        else:
            entries[key] = ("file", path.read_bytes())
    return entries


@pytest.mark.parametrize("level", _PARENT_LEVELS)
@pytest.mark.parametrize("boundary", ["link", "unlink"])
@pytest.mark.parametrize("action", ["archive", "restore"])
def test_a_parent_swapped_for_a_symlink_after_validation_cannot_redirect_the_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, boundary: str, level: str
) -> None:
    """Swap a checked parent for a symlink to an outside tree at the instant of the link or of the unlink.

    The outside tree holds a victim at the source's name. The move must complete inside the real tree
    (the handles were taken before the swap) and neither write nor remove anything outside it.
    """
    home = tmp_path / "home"
    memory = home / "projects" / "demo" / "memory"
    (memory / "archive").mkdir(parents=True)
    source = memory / "notes.md" if action == "archive" else memory / "archive" / ARCHIVED
    source.write_text("selected source\n", encoding="utf-8")
    outside = tmp_path / "outside"
    swap = home / level
    if swap == memory / "archive":
        redirected_memory, redirected_archive = memory, outside
    else:
        redirected_memory = outside / memory.relative_to(swap)
        redirected_archive = redirected_memory / "archive"
    redirected_archive.mkdir(parents=True)
    victim = redirected_memory / "notes.md" if action == "archive" else redirected_archive / ARCHIVED
    if victim != source:
        victim.write_text("outside victim\n", encoding="utf-8")
    outside_before = _tree(outside)
    parked = tmp_path / "parked"
    real = getattr(os, boundary)

    def interleave(*args: Any, **kwargs: Any) -> Any:
        swap.rename(parked)
        swap.symlink_to(outside, target_is_directory=True)
        return real(*args, **kwargs)

    monkeypatch.setattr(archive.os, boundary, interleave)
    if action == "archive":
        result = archive.archive(home, "demo", "notes.md", now=FIXED_NOW)
    else:
        result = archive.restore(home, "demo", ARCHIVED)
    monkeypatch.undo()

    assert result["status"] == ("archived" if action == "archive" else "restored")
    assert swap.is_symlink()
    assert _tree(outside) == outside_before
    if swap == memory / "archive":
        real_memory, real_archive = memory, parked
    else:
        real_memory = parked / memory.relative_to(swap)
        real_archive = real_memory / "archive"
    moved_to = real_archive / ARCHIVED if action == "archive" else real_memory / "notes.md"
    moved_from = real_memory / "notes.md" if action == "archive" else real_archive / ARCHIVED
    assert moved_to.read_text(encoding="utf-8") == "selected source\n"
    assert not moved_from.exists()


def test_a_symlink_planted_at_the_source_name_after_the_check_is_not_moved(
    project_root: Tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The new link must be the regular file that was checked; a symlink planted at the source name meanwhile
    is linked as itself (follow_symlinks=False), detected after the link, undone, and the move refused.

    A regular file planted at the name is not always distinguishable: some filesystems (ext4) reuse a freed
    inode number at once, so that case is not asserted here. Either way the name lies inside the validated
    memory directory, so containment is unaffected.
    """
    home, project = project_root
    source = project / "memory" / "notes.md"
    source.write_text("checked\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("elsewhere\n", encoding="utf-8")

    def plant_symlink() -> None:
        source.unlink()
        source.symlink_to(elsewhere)

    _interleave(monkeypatch, plant_symlink)
    with pytest.raises(archive.ArchiveError, match="changed after it was checked"):
        archive.archive(home, "demo", "notes.md", now=FIXED_NOW)

    assert source.is_symlink()
    assert elsewhere.read_text(encoding="utf-8") == "elsewhere\n"
    assert not (project / "memory" / "archive" / ARCHIVED).exists()


# --- protected identity: the active files by inode, not by spelling ----------


@pytest.mark.parametrize("protected", layout.PROJECT.files)
def test_a_name_that_addresses_an_active_file_on_this_filesystem_is_refused(
    project_root: Tuple[Path, Path], protected: str
) -> None:
    home, project = project_root
    memory = project / "memory"
    (memory / protected).write_text("# active\n", encoding="utf-8")
    alternate = protected.upper()
    if not (memory / alternate).exists():
        # case-sensitive filesystem: give the alternate spelling the same inode the way such a filesystem can
        os.link(memory / protected, memory / alternate)
    assert (memory / alternate).stat().st_ino == (memory / protected).stat().st_ino

    for dry_run in (True, False):
        with pytest.raises(archive.ArchiveError, match="cannot archive standard active memory file"):
            archive.archive(home, "demo", alternate, dry_run=dry_run, now=FIXED_NOW)
    assert (memory / protected).read_text(encoding="utf-8") == "# active\n"
    assert not list((memory / "archive").iterdir())

    # the alternate spelling cannot be restored over the occupied slot either
    (memory / "archive" / f"20260320T010203Z_{alternate}").write_text("old\n", encoding="utf-8")
    with pytest.raises(archive.ArchiveError, match="active memory destination already exists"):
        archive.restore(home, "demo", f"20260320T010203Z_{alternate}")
    assert (memory / protected).read_text(encoding="utf-8") == "# active\n"

    # a supplementary file beside them still archives
    (memory / "notes.md").write_text("notes\n", encoding="utf-8")
    assert archive.archive(home, "demo", "notes.md", now=FIXED_NOW)["status"] == "archived"


# --- the archive path: absent, directory, regular file, symlink; dry run and real


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("state", ["absent", "directory", "regular_file", "symlink"])
def test_the_archive_path_is_validated_before_dry_run_and_real_run_diverge(
    tmp_path: Path, state: str, dry_run: bool
) -> None:
    home = tmp_path / "home"
    memory = home / "projects" / "demo" / "memory"
    memory.mkdir(parents=True)
    (memory / "notes.md").write_text("notes\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    if state == "directory":
        (memory / "archive").mkdir()
    elif state == "regular_file":
        (memory / "archive").write_text("regular file\n", encoding="utf-8")
    elif state == "symlink":
        (memory / "archive").symlink_to(outside, target_is_directory=True)

    if state in ("absent", "directory"):
        missing = "memory archive directory not found" if state == "absent" else "archived memory file not found"
        with pytest.raises(archive.ArchiveError, match=missing):
            archive.restore(home, "demo", ARCHIVED, dry_run=dry_run)
        result = archive.archive(home, "demo", "notes.md", dry_run=dry_run, now=FIXED_NOW)
        assert result["status"] == ("dry-run" if dry_run else "archived")
        assert (memory / "archive" / ARCHIVED).is_file() is not dry_run
        assert (memory / "archive").is_dir() is (state == "directory" or not dry_run)
    else:
        expected = "archive directory .* is not a directory" if state == "regular_file" else "archive directory .* is a symlink"
        with pytest.raises(archive.ArchiveError, match=expected):
            archive.archive(home, "demo", "notes.md", dry_run=dry_run, now=FIXED_NOW)
        with pytest.raises(archive.ArchiveError, match=expected):
            archive.restore(home, "demo", ARCHIVED, dry_run=dry_run)
        assert (memory / "notes.md").read_text(encoding="utf-8") == "notes\n"
        if state == "regular_file":
            assert (memory / "archive").read_text(encoding="utf-8") == "regular file\n"
    assert not list(outside.iterdir())


def test_a_regular_file_at_the_archive_path_is_a_controlled_error_on_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    memory = home / "projects" / "demo" / "memory"
    memory.mkdir(parents=True)
    (memory / "notes.md").write_text("notes\n", encoding="utf-8")
    (memory / "archive").write_text("regular file\n", encoding="utf-8")

    for extra in (["--dry-run"], []):
        assert main(["archive", "demo", "notes.md", "--home", str(home), *extra]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("agent-memory: error: archive directory ")
        assert "is not a directory" in captured.err and "Traceback" not in captured.err
    assert main(["restore", "demo", ARCHIVED, "--home", str(home)]) == 1
    assert "is not a directory" in capsys.readouterr().err
    assert (memory / "notes.md").read_text(encoding="utf-8") == "notes\n"
