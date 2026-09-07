from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Dict

import pytest

from agent_memory import layout

GOLDEN = Path(__file__).resolve().parent / "golden" / "canonical_memory_gitignore.txt"


def _digest(root: Path) -> Dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir"
        for path in sorted(root.rglob("*"))
    }


def test_gitignore_text_matches_the_golden_bytes() -> None:
    # tests/golden/canonical_memory_gitignore.txt is a byte copy of the 0.4.5 kernel's canonical text.
    assert layout.gitignore_text().encode("utf-8") == GOLDEN.read_bytes()


def test_gitignore_denies_keystore_last() -> None:
    lines = layout.gitignore_text().splitlines()
    assert "keys/" in lines
    # The deny must come after every allowlist line so it wins for keys/
    # even if a future edit widens the allowlist above it.
    assert lines.index("keys/") > max(index for index, line in enumerate(lines) if line.startswith("!"))


def test_marker_and_gitignore_names_are_the_fleet_contract() -> None:
    assert layout.MARKER_FILE == ".oacp-memory-repo"
    assert layout.GITIGNORE_FILE == ".gitignore"


@pytest.mark.parametrize(
    "path",
    [
        ".gitignore",
        ".oacp-memory-repo",
        "org-memory/recent.md",
        "org-memory/debriefs/.gitkeep",
        "org-memory/debriefs/demo-project/2026/08/20260825-alice-1f3a9c2b.md",
        "projects/demo/memory/project_facts.md",
        "projects/demo/memory/archive/20260101T000000Z_open_threads.md",
        "projects/demo/memory/keys.md",
    ],
)
def test_allowed_memory_paths(path: str) -> None:
    assert layout.is_allowed_memory_path(path), path


@pytest.mark.parametrize(
    "path",
    [
        "keys/",
        "keys/00000000-0000-4000-8000-000000000000/claude/00000000-0000-4000-8000-000000000001/kid.json",
        "keys/domain/claude/instance/kid.pub.json",
        "keys/.trust_domain",
        "org-memory",
        "projects/demo/memory",
        "projects/demo/memory/.cache",
        "projects/demo/memory/.cache/index.json",
        "projects/demo/agents/claude/inbox/msg.yaml",
        "projects/demo/status.yaml",
        "agents/claude/config.yaml",
        "README.md",
        "state/watch/cursor",
    ],
)
def test_denied_memory_paths(path: str) -> None:
    assert not layout.is_allowed_memory_path(path), path


def test_every_tier_file_and_dir_is_an_allowed_memory_path() -> None:
    for tier in layout.TIERS:
        root = tier.pattern.replace(layout.WILDCARD, "demo")
        for name in tier.files + tier.dirs:
            assert layout.is_allowed_memory_path(f"{root}/{name}"), name
        for name in tier.unsynced:
            assert not layout.is_allowed_memory_path(f"{root}/{name}/x"), name


def test_allowed_memory_dirs_lists_existing_tiers_in_allowlist_order(tmp_path: Path) -> None:
    (tmp_path / "org-memory").mkdir()
    for name in ("zeta", "alpha"):
        (tmp_path / "projects" / name / "memory").mkdir(parents=True)
    (tmp_path / "projects" / "no-memory").mkdir()
    (tmp_path / "projects" / "stray.txt").write_text("", encoding="utf-8")
    assert layout.allowed_memory_dirs(tmp_path) == [
        tmp_path / "org-memory",
        tmp_path / "projects" / "alpha" / "memory",
        tmp_path / "projects" / "zeta" / "memory",
    ]


def test_allowed_memory_dirs_on_an_empty_or_missing_home(tmp_path: Path) -> None:
    assert layout.allowed_memory_dirs(tmp_path) == []
    assert layout.allowed_memory_dirs(tmp_path / "missing") == []


def test_tier_dirs_derive_from_the_table(tmp_path: Path) -> None:
    assert layout.org_memory_dir(tmp_path) == tmp_path / "org-memory"
    assert layout.project_memory_dir(tmp_path, "demo") == tmp_path / "projects" / "demo" / "memory"


@pytest.mark.parametrize("bad", ["", ".hidden", "a/b", "a\\b", "../up"])
def test_project_names_with_separators_or_leading_dots_are_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        layout.project_memory_dir(tmp_path, bad)


def test_scaffold_home_creates_the_layout_once(tmp_path: Path) -> None:
    home = tmp_path / "home"
    created = layout.scaffold_home(home)
    assert created[0] == home
    assert (home / ".gitignore").read_bytes() == GOLDEN.read_bytes()
    assert (home / "org-memory" / "events").is_dir()
    assert (home / "org-memory" / "debriefs").is_dir()
    assert (home / "projects").is_dir()
    assert not (home / layout.MARKER_FILE).exists()  # syncing is opt-in, not part of the layout
    before = _digest(home)
    assert layout.scaffold_home(home) == []
    assert _digest(home) == before


def test_scaffold_home_with_a_project_tier(tmp_path: Path) -> None:
    home = tmp_path / "home"
    created = layout.scaffold_home(home, project="demo")
    memory = home / "projects" / "demo" / "memory"
    assert memory in created
    assert (memory / "archive").is_dir()
    assert not any(path.is_file() for path in memory.rglob("*"))  # content belongs to the scaffolding verbs
    assert layout.scaffold_home(home, project="demo") == []
    other = home / "projects" / "other" / "memory"
    assert layout.scaffold_home(home, project="other") == [other, other / "archive"]


def _occupy(slot: Path, shape: str, outside: Path) -> None:
    if shape == "regular file":
        slot.write_bytes(b"custom\n")
    elif shape == "directory":
        slot.mkdir()
    else:
        slot.symlink_to(outside)


def _assert_untouched(slot: Path, shape: str, outside: Path) -> None:
    if shape == "regular file":
        assert slot.read_bytes() == b"custom\n"
    elif shape == "directory":
        assert slot.is_dir()
    else:
        assert os.readlink(slot) == str(outside)
        assert not outside.exists()


@pytest.mark.parametrize("shape", ["regular file", "directory", "dangling symlink"])
def test_scaffold_home_keeps_whatever_occupies_the_gitignore_slot(tmp_path: Path, shape: str) -> None:
    """The slot is never rewritten and a link in it is never followed: no bytes land at its target."""
    home = tmp_path / "home"
    home.mkdir()
    slot, outside = home / ".gitignore", tmp_path / "outside"
    _occupy(slot, shape, outside)
    created = layout.scaffold_home(home)
    assert slot not in created
    _assert_untouched(slot, shape, outside)


@pytest.mark.parametrize("shape", ["regular file", "dangling symlink"])
def test_scaffold_home_keeps_a_gitignore_that_appears_after_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """The existence probe is advisory; the exclusive open is the guarantee."""
    home = tmp_path / "home"
    slot, outside = home / ".gitignore", tmp_path / "outside"
    real_lexists = os.path.lexists

    def probe_then_lose_the_race(path: object) -> bool:
        if Path(path) == slot and not real_lexists(slot):
            _occupy(slot, shape, outside)
            return False  # what the probe saw an instant ago
        return real_lexists(path)

    monkeypatch.setattr(os.path, "lexists", probe_then_lose_the_race)
    created = layout.scaffold_home(home)
    assert slot not in created
    _assert_untouched(slot, shape, outside)


def test_write_if_absent_creates_a_missing_file_once(tmp_path: Path) -> None:
    path = tmp_path / "file"
    assert layout.write_if_absent(path, b"first\n") is True
    assert layout.write_if_absent(path, b"second\n") is False
    assert path.read_bytes() == b"first\n"
    with pytest.raises(OSError):
        layout.write_if_absent(tmp_path / "missing-dir" / "file", b"")


@pytest.mark.parametrize(
    "path",
    [
        "org-memory/keys/k.json",
        "org-memory/debriefs/keys/k.json",
        "projects/demo/memory/keys/k.json",
        "projects/demo/memory/archive/keys/k.json",
        "projects/demo/memory/keys",
        "projects/keys/memory/facts.md",
    ],
)
def test_a_never_synced_name_is_denied_at_any_depth(path: str) -> None:
    # The ignore file can be widened by hand; the predicate is what the sync engine trusts.
    assert not layout.is_allowed_memory_path(path), path


@pytest.mark.parametrize("path", ["projects/demo/memory/keys.md", "org-memory/keys-rotation.md", "org-memory/my-keys/x.md"])
def test_names_that_merely_contain_a_never_synced_name_stay_allowed(path: str) -> None:
    assert layout.is_allowed_memory_path(path), path
