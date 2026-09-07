# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""``agent-memory init`` and ``org init``: the two-tier layout from bundled templates, and nothing else.

The grammar: templates load through the public resources API and a missing one
fails loud before any write; a rerun leaves no diff; a binding collision is
refused before any write; no git, no network, no credentials. The installed
wheel proves the template path (``AGENT_MEMORY_TEST_INSTALLED=1``).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path
from typing import Dict, Set

import pytest

from agent_memory import layout, org
from agent_memory.cli import main
from agent_memory.home import BINDING_FILE, ENV_COMPAT_HOME, ENV_HOME, resolve_home

CHECKOUT = Path(__file__).resolve().parents[1]
TEMPLATES = CHECKOUT / "src" / "agent_memory" / "templates"
INSTALLED_ENV = "AGENT_MEMORY_TEST_INSTALLED"

not_root = pytest.mark.skipif(os.geteuid() == 0, reason="permission bits ignored as root")

ORG_TREE: Set[str] = {
    ".gitignore",
    "projects/",
    "org-memory/",
    "org-memory/recent.md",
    "org-memory/decisions.md",
    "org-memory/rules.md",
    "org-memory/events/",
    "org-memory/events/.gitkeep",
    "org-memory/debriefs/",
    "org-memory/debriefs/.gitkeep",
}


def _project_tree(project: str) -> Set[str]:
    memory = f"projects/{project}/memory"
    return {f"projects/{project}/", f"{memory}/", f"{memory}/archive/"} | {f"{memory}/{name}" for name in layout.PROJECT.files}


def _tree(root: Path) -> Dict[str, str]:
    """Every entry under ``root``: directories as ``rel/`` mapping to ``dir``, files to their digest."""
    tree: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            tree[f"{rel}/"] = "dir"
        else:
            tree[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return tree


def _template(tier: layout.Tier, name: str) -> bytes:
    return (TEMPLATES / org.TEMPLATE_DIRS[tier.name] / name).read_bytes()


@pytest.fixture
def no_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No git on PATH, and any subprocess is a test failure: the scaffold is filesystem only."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"a subprocess was started: {args[0] if args else kwargs}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


# --- the layout ---------------------------------------------------------------


def test_a_fresh_home_is_the_full_two_tier_layout(tmp_path: Path, no_git: None) -> None:
    home = tmp_path / "home"
    report = org.init(home, project="demo")
    assert set(_tree(home)) == ORG_TREE | _project_tree("demo")
    for name in layout.ORG.files:
        assert (layout.org_memory_dir(home) / name).read_bytes() == _template(layout.ORG, name)
    for name in layout.PROJECT.files:
        assert (layout.project_memory_dir(home, "demo") / name).read_bytes() == _template(layout.PROJECT, name)
    # The bug this fixes: an installed 0.4.5 wrote a nine-byte heading-only recent.md from a silent fallback.
    assert (layout.org_memory_dir(home) / "recent.md").stat().st_size == len(_template(layout.ORG, "recent.md")) > 100
    assert (home / ".gitignore").read_bytes() == layout.gitignore_text().encode("utf-8")
    assert not (home / ".git").exists()
    assert set(report.created) == (ORG_TREE | _project_tree("demo")) - {"projects/demo/"}
    assert report.kept == () and report.binding is None and report.project == "demo"
    assert report.lines()[0] == f"Initialized memory home: {home}"


def test_init_without_a_project_creates_the_org_tier_only(tmp_path: Path, no_git: None) -> None:
    home = tmp_path / "home"
    report = org.init(home)
    assert set(_tree(home)) == ORG_TREE
    assert report.project is None


def test_a_rerun_changes_no_byte_and_reports_what_it_kept(tmp_path: Path, no_git: None) -> None:
    home = tmp_path / "home"
    org.init(home, project="demo")
    before = _tree(home)
    report = org.init(home, project="demo")
    assert _tree(home) == before
    assert report.created == ()
    # The layout (directories, .gitignore) is silent when present; the tier files are what a rerun reports.
    assert set(report.kept) == {entry for entry in ORG_TREE | _project_tree("demo") if not entry.endswith("/")} - {".gitignore"}
    assert report.changed is False
    assert report.lines()[0] == f"Memory home already complete: {home}"


def test_existing_files_are_never_overwritten(tmp_path: Path, no_git: None) -> None:
    home = tmp_path / "home"
    recent = layout.org_memory_dir(home) / "recent.md"
    recent.parent.mkdir(parents=True)
    recent.write_text("# mine\n\nhand-written\n", encoding="utf-8")
    (home / ".gitignore").write_text("*.swp\n", encoding="utf-8")
    report = org.init(home)
    assert recent.read_text(encoding="utf-8") == "# mine\n\nhand-written\n"
    assert (home / ".gitignore").read_text(encoding="utf-8") == "*.swp\n"
    assert "org-memory/recent.md" in report.kept
    assert "org-memory/decisions.md" in report.created


def test_an_entry_in_a_file_slot_is_kept_not_replaced(tmp_path: Path, no_git: None) -> None:
    home = tmp_path / "home"
    slot = layout.org_memory_dir(home) / "rules.md"
    slot.mkdir(parents=True)
    report = org.init(home)
    assert slot.is_dir()
    assert "org-memory/rules.md/" in report.kept


def test_a_home_path_that_is_a_file_is_a_controlled_error(tmp_path: Path, no_git: None) -> None:
    occupied = tmp_path / "home"
    occupied.write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(org.ScaffoldError, match="cannot lay out"):
        org.init(occupied)
    assert occupied.read_text(encoding="utf-8") == "not a directory\n"


def test_a_dangling_symlink_in_a_file_slot_is_kept_and_not_followed(tmp_path: Path, no_git: None) -> None:
    home = tmp_path / "home"
    org_dir = layout.org_memory_dir(home)
    org_dir.mkdir(parents=True)
    (org_dir / "recent.md").symlink_to(tmp_path / "outside.md")
    report = org.init(home)
    assert "org-memory/recent.md" in report.kept
    assert not (tmp_path / "outside.md").exists()


def test_a_dangling_symlink_in_the_root_gitignore_slot_is_kept_and_not_followed(tmp_path: Path, no_git: None) -> None:
    """The root .gitignore slot is a file slot like any other: a follow-the-link probe once wrote the allowlist to its target."""
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside.gitignore"
    (home / ".gitignore").symlink_to(outside)
    report = org.init(home)
    assert os.readlink(home / ".gitignore") == str(outside)
    assert not outside.exists()
    assert ".gitignore" not in report.created
    assert "org-memory/recent.md" in report.created


# --- templates ----------------------------------------------------------------


def _templates_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "templates-copy"
    shutil.copytree(TEMPLATES, copy)
    return copy


def test_every_template_is_read_before_the_first_write(tmp_path: Path, no_git: None, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = _templates_copy(tmp_path)
    (copy / "project-memory" / "known_debt.md").unlink()
    monkeypatch.setattr(org, "_templates_root", lambda: copy)
    home = tmp_path / "home"
    with pytest.raises(org.ScaffoldError, match="templates/project-memory/known_debt.md is missing from the installed package"):
        org.init(home)  # the project tier was not even requested
    assert not home.exists()


@not_root
def test_an_unreadable_template_fails_loud(tmp_path: Path, no_git: None, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = _templates_copy(tmp_path)
    blocked = copy / "org-memory" / "decisions.md"
    os.chmod(blocked, 0o000)
    monkeypatch.setattr(org, "_templates_root", lambda: copy)
    home = tmp_path / "home"
    try:
        with pytest.raises(org.ScaffoldError, match="templates/org-memory/decisions.md cannot be read"):
            org.init(home)
    finally:
        os.chmod(blocked, 0o644)
    assert not home.exists()


def test_templates_come_from_the_package_through_the_resources_api() -> None:
    root = org._templates_root()
    assert root == resources.files("agent_memory") / org.TEMPLATES_DIR
    for tier in layout.TIERS:
        for name in tier.files:
            assert org.template_bytes(tier, name) == _template(tier, name)
            assert len(org.template_bytes(tier, name)) > 20


def test_templates_load_from_the_installed_wheel(tmp_path: Path) -> None:
    if os.environ.get(INSTALLED_ENV) != "1":
        pytest.skip(f"{INSTALLED_ENV}=1 not set: source-checkout run")
    location = Path(str(resources.files("agent_memory"))).resolve()
    assert "site-packages" in location.parts and CHECKOUT not in location.parents
    for tier in layout.TIERS:
        for name in tier.files:
            assert org.template_bytes(tier, name) == _template(tier, name)
    exe = shutil.which("agent-memory")
    assert exe, "agent-memory console script not on PATH"
    home = tmp_path / "home"
    env = {key: value for key, value in os.environ.items() if key not in (ENV_HOME, ENV_COMPAT_HOME)}
    result = subprocess.run([exe, "init", "--home", str(home), "--project", "demo"], capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 0, result.stderr
    assert (layout.org_memory_dir(home) / "recent.md").read_bytes() == _template(layout.ORG, "recent.md")
    assert (layout.project_memory_dir(home, "demo") / "known_debt.md").read_bytes() == _template(layout.PROJECT, "known_debt.md")


# --- the binding --------------------------------------------------------------


def test_a_binding_is_recorded_last_and_the_resolver_finds_it(tmp_path: Path, no_git: None) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    report = org.init(home, project="demo", repo=repo)
    binding = repo / BINDING_FILE
    assert report.binding == binding and report.binding_action == "created"
    assert json.loads(binding.read_text(encoding="utf-8")) == {"schema_version": 1, "project": "demo", "home": str(home)}
    found = resolve_home(env={}, cwd=repo / "src" / "deep")
    assert (found.path, found.project, found.source) == (home, "demo", f"binding:{binding}")
    assert any(line.startswith(f"binding recorded: {binding} -> {home}") for line in report.lines())
    assert any("machine-local" in line for line in report.lines())


def test_the_project_derives_from_the_repository_name(tmp_path: Path, no_git: None) -> None:
    repo = tmp_path / "my-service"
    repo.mkdir()
    report = org.init(tmp_path / "home", repo=repo)
    assert report.project == "my-service"
    assert (layout.project_memory_dir(tmp_path / "home", "my-service") / "project_facts.md").is_file()


def test_an_identical_binding_is_left_alone(tmp_path: Path, no_git: None) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    org.init(home, project="demo", repo=repo)
    binding = repo / BINDING_FILE
    before = binding.read_bytes()
    report = org.init(home, project="demo", repo=repo)
    assert report.binding_action == "unchanged" and report.changed is False
    assert binding.read_bytes() == before
    assert any(line.startswith("binding already recorded:") for line in report.lines())


@pytest.mark.parametrize("difference", ["home", "project"])
def test_a_binding_pointing_elsewhere_is_refused_before_any_write(tmp_path: Path, no_git: None, difference: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    other = {"schema_version": 1, "project": "demo" if difference == "home" else "other", "home": str(tmp_path / ("elsewhere" if difference == "home" else "home"))}
    binding = repo / BINDING_FILE
    binding.write_text(json.dumps(other), encoding="utf-8")
    before = binding.read_bytes()
    home = tmp_path / "home"
    with pytest.raises(org.ScaffoldError, match="collision: .* already binds this repository"):
        org.init(home, project="demo", repo=repo)
    assert not home.exists()
    assert binding.read_bytes() == before


@pytest.mark.parametrize("shape", ["directory", "malformed", "dangling symlink"])
def test_an_unusable_binding_entry_is_a_collision(tmp_path: Path, no_git: None, shape: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    entry = repo / BINDING_FILE
    if shape == "directory":
        entry.mkdir()
    elif shape == "malformed":
        entry.write_text("{", encoding="utf-8")
    else:
        entry.symlink_to(repo / "missing.json")
    home = tmp_path / "home"
    with pytest.raises(org.ScaffoldError, match="collision: .*; not overwriting"):
        org.init(home, project="demo", repo=repo)
    assert not home.exists()
    assert os.path.lexists(entry)


def test_the_repository_must_be_a_directory(tmp_path: Path, no_git: None) -> None:
    home = tmp_path / "home"
    with pytest.raises(org.ScaffoldError, match="is not a directory"):
        org.init(home, project="demo", repo=tmp_path / "nope")
    assert not home.exists()


@pytest.mark.parametrize("project", ["", ".hidden", "a/b", "a\\b"])
def test_an_invalid_project_name_is_refused_before_any_write(tmp_path: Path, no_git: None, project: str) -> None:
    home = tmp_path / "home"
    with pytest.raises(org.ScaffoldError, match="project"):
        org.init(home, project=project)
    assert not home.exists()


def test_an_underivable_project_name_asks_for_the_flag(tmp_path: Path, no_git: None) -> None:
    repo = tmp_path / ".dotrepo"
    repo.mkdir()
    with pytest.raises(org.ScaffoldError, match="pass --project"):
        org.init(tmp_path / "home", repo=repo)
    assert not (tmp_path / "home").exists()


# --- org init -------------------------------------------------------------------


def test_org_init_requires_an_existing_home(tmp_path: Path, no_git: None) -> None:
    with pytest.raises(org.ScaffoldError, match="is not a directory"):
        org.org_init(tmp_path / "home")
    assert not (tmp_path / "home").exists()


def test_org_init_completes_the_org_tier_and_touches_no_project(tmp_path: Path, no_git: None) -> None:
    home = tmp_path / "home"
    layout.scaffold_home(home, project="demo")  # directories only, no tier files
    report = org.org_init(home)
    assert set(report.created) == {"org-memory/recent.md", "org-memory/decisions.md", "org-memory/rules.md", "org-memory/events/.gitkeep", "org-memory/debriefs/.gitkeep"}
    assert not any((layout.project_memory_dir(home, "demo") / name).exists() for name in layout.PROJECT.files)
    assert report.project is None


# --- the command line -----------------------------------------------------------


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.delenv(ENV_HOME, raising=False)
    monkeypatch.delenv(ENV_COMPAT_HOME, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_cli_init_reports_and_exits_zero(isolated: Path, no_git: None, capsys: pytest.CaptureFixture[str]) -> None:
    home = isolated / "home"
    repo = isolated / "repo"
    repo.mkdir()
    assert main(["init", "--home", str(home), "--project", "demo", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"Initialized memory home: {home}"
    assert "  + org-memory/recent.md" in out
    assert "project: demo" in out
    assert any(line.startswith(f"binding recorded: {repo / BINDING_FILE}") for line in out)

    assert main(["init", "--home", str(home), "--project", "demo", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"Memory home already complete: {home}"
    assert "  (exists) org-memory/recent.md" in out


def test_cli_init_uses_the_default_home_when_nothing_names_one(
    isolated: Path, no_git: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(isolated))
    assert main(["init"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == f"Initialized memory home: {isolated / 'agent-memory'}"
    assert set(_tree(isolated / "agent-memory")) == ORG_TREE


def test_cli_org_init(isolated: Path, no_git: None, capsys: pytest.CaptureFixture[str]) -> None:
    home = isolated / "home"
    assert main(["org", "init", "--home", str(home)]) == 1
    assert "agent-memory: error:" in capsys.readouterr().err
    home.mkdir()
    assert main(["org", "init", "--home", str(home)]) == 0
    assert "  + org-memory/rules.md" in capsys.readouterr().out
    assert set(_tree(home)) == ORG_TREE


def test_cli_collision_is_a_controlled_error(isolated: Path, no_git: None, capsys: pytest.CaptureFixture[str]) -> None:
    repo = isolated / "repo"
    repo.mkdir()
    (repo / BINDING_FILE).write_text(json.dumps({"schema_version": 1, "home": str(isolated / "other")}), encoding="utf-8")
    assert main(["init", "--home", str(isolated / "home"), "--project", "demo", "--repo", str(repo)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("agent-memory: error: collision:")
    assert not (isolated / "home").exists()
