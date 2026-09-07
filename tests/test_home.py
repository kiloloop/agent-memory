from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_memory.home import (
    BINDING_FILE,
    DEFAULT_HOME,
    ENV_COMPAT_HOME,
    ENV_HOME,
    HomeError,
    find_workspace_marker,
    load_binding,
    resolve_home,
)


def _workspace(home: Path, project: str) -> Path:
    workspace = home / "projects" / project / "workspace.json"
    workspace.parent.mkdir(parents=True)
    workspace.write_text("{}\n", encoding="utf-8")
    return workspace


def _binding(directory: Path, **fields: object) -> Path:
    path = directory / BINDING_FILE
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def test_explicit_flag_wins_over_everything(tmp_path: Path) -> None:
    env = {ENV_HOME: str(tmp_path / "env-home")}
    found = resolve_home(str(tmp_path / "flag-home"), env=env, cwd=tmp_path)
    assert found.path == tmp_path / "flag-home"
    assert found.source == "flag"


def test_agent_memory_home_env(tmp_path: Path) -> None:
    found = resolve_home(env={ENV_HOME: str(tmp_path / "h")}, cwd=tmp_path)
    assert found.path == tmp_path / "h"
    assert found.source == f"env:{ENV_HOME}"


def test_compat_env_recognized_but_outranked(tmp_path: Path) -> None:
    compat_only = resolve_home(env={ENV_COMPAT_HOME: str(tmp_path / "compat")}, cwd=tmp_path)
    assert compat_only.path == tmp_path / "compat"
    assert compat_only.source == f"env:{ENV_COMPAT_HOME}"
    both = resolve_home(
        env={ENV_HOME: str(tmp_path / "own"), ENV_COMPAT_HOME: str(tmp_path / "compat")},
        cwd=tmp_path,
    )
    assert both.path == tmp_path / "own"


def test_empty_env_values_fall_through(tmp_path: Path) -> None:
    found = resolve_home(env={ENV_HOME: "", ENV_COMPAT_HOME: ""}, cwd=tmp_path)
    assert found.source == "default"


def test_tilde_expands_against_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    found = resolve_home(env={ENV_HOME: "~/mem"}, cwd=tmp_path)
    assert found.path == tmp_path / "mem"
    assert resolve_home("~/flag", env={}, cwd=tmp_path).path == tmp_path / "flag"


def test_default_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    found = resolve_home(env={}, cwd=tmp_path)
    assert found.path == tmp_path / "agent-memory"
    assert found.source == "default"
    assert DEFAULT_HOME == "~/agent-memory"


def test_binding_found_walking_up(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    deep = repo / "a" / "b"
    deep.mkdir(parents=True)
    binding = _binding(repo, schema_version=1, project="demo", home=str(tmp_path / "store"))
    found = resolve_home(env={}, cwd=deep)
    assert found.path == tmp_path / "store"
    assert found.project == "demo"
    assert found.source == f"binding:{binding}"


def test_binding_relative_home_is_relative_to_the_binding(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _binding(repo, schema_version=1, home="../store")
    found = resolve_home(env={}, cwd=repo)
    assert found.path == repo / "../store"
    assert found.project is None


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        json.dumps({"schema_version": 2, "home": "/x"}),
        json.dumps({"schema_version": True, "home": "/x"}),  # bool is not an int here, even though True == 1
        json.dumps({"schema_version": False, "home": "/x"}),
        json.dumps({"schema_version": 1.0, "home": "/x"}),
        json.dumps({"schema_version": "1", "home": "/x"}),
        json.dumps({"schema_version": None, "home": "/x"}),
        json.dumps({"home": "/x"}),
        json.dumps({"schema_version": 1}),
        json.dumps({"schema_version": 1, "home": ""}),
        json.dumps({"schema_version": 1, "home": "/x", "project": 7}),
        json.dumps({"schema_version": 1, "home": "/x", "project": ""}),
    ],
)
def test_bad_binding_is_an_error_not_a_fallthrough(tmp_path: Path, content: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / BINDING_FILE).write_text(content, encoding="utf-8")
    with pytest.raises(HomeError):
        resolve_home(env={}, cwd=repo)
    with pytest.raises(HomeError):
        load_binding(repo / BINDING_FILE)


def test_nearest_binding_wins(tmp_path: Path) -> None:
    _binding(tmp_path, schema_version=1, home=str(tmp_path / "outer-store"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _binding(repo, schema_version=1, home=str(tmp_path / "inner-store"))
    assert resolve_home(env={}, cwd=repo).path == tmp_path / "inner-store"
    assert resolve_home(env={}, cwd=tmp_path).path == tmp_path / "outer-store"


def test_binding_symlink_to_a_valid_file_is_followed(tmp_path: Path) -> None:
    target = tmp_path / "shared" / "binding.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"schema_version": 1, "project": "demo", "home": str(tmp_path / "store")}))
    repo = tmp_path / "repo"
    repo.mkdir()
    link = repo / BINDING_FILE
    link.symlink_to(target)
    found = resolve_home(env={}, cwd=repo)
    assert found.path == tmp_path / "store"
    assert found.project == "demo"
    assert found.source == f"binding:{link}"


BROKEN_ENTRY_KINDS = ("dangling symlink", "symlink loop", "directory", "symlink to a directory", "unreadable file")


def _broken_binding_entry(directory: Path, kind: str) -> Path:
    """Put something at the binding's name that is not a readable file."""
    path = directory / BINDING_FILE
    if kind == "dangling symlink":
        path.symlink_to(directory / "missing.json")
    elif kind == "symlink loop":
        path.symlink_to(path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "symlink to a directory":
        path.symlink_to(directory)
    elif kind == "unreadable file":
        path.write_text(json.dumps({"schema_version": 1, "home": str(directory / "store")}), encoding="utf-8")
        path.chmod(0)
        if os.access(path, os.R_OK):
            pytest.skip("this user reads files regardless of their mode bits")
    return path


@pytest.mark.parametrize("ancestor_binding", [True, False], ids=["with-ancestor-binding", "no-ancestor-binding"])
@pytest.mark.parametrize("kind", BROKEN_ENTRY_KINDS)
def test_broken_nearer_binding_entry_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, ancestor_binding: bool
) -> None:
    """An unusable entry at the binding's name is an error; it never selects an ancestor or the default."""
    monkeypatch.setenv("HOME", str(tmp_path))
    if ancestor_binding:
        _binding(tmp_path, schema_version=1, home=str(tmp_path / "outer-store"))
    repo = tmp_path / "repo"
    repo.mkdir()
    path = _broken_binding_entry(repo, kind)
    try:
        with pytest.raises(HomeError, match=BINDING_FILE.replace(".", "[.]")):
            resolve_home(env={}, cwd=repo)
        with pytest.raises(HomeError):
            load_binding(path)
        if ancestor_binding:
            assert resolve_home(env={}, cwd=tmp_path).path == tmp_path / "outer-store"
    finally:
        if kind == "unreadable file":
            path.chmod(0o600)


def test_binding_outranks_marker(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "home", "demo")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".oacp").symlink_to(workspace)
    _binding(repo, schema_version=1, home=str(tmp_path / "bound"))
    assert resolve_home(env={}, cwd=repo).path == tmp_path / "bound"


def test_marker_symlink_found_walking_up_whatever_its_name(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "home", "demo")
    repo = tmp_path / "repo"
    deep = repo / "src" / "pkg"
    deep.mkdir(parents=True)
    link = repo / ".oacp"
    link.symlink_to(workspace)
    found = resolve_home(env={}, cwd=deep)
    assert found.path == (tmp_path / "home").resolve()
    assert found.source == f"marker:{link}"
    link.rename(repo / "any-name")
    assert resolve_home(env={}, cwd=deep).path == (tmp_path / "home").resolve()


def test_nearest_marker_wins(tmp_path: Path) -> None:
    inner = _workspace(tmp_path / "inner-home", "p")
    outer = _workspace(tmp_path / "outer-home", "p")
    repo = tmp_path / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    (repo / ".oacp").symlink_to(outer)
    (sub / ".oacp").symlink_to(inner)
    assert resolve_home(env={}, cwd=sub).path == (tmp_path / "inner-home").resolve()
    assert resolve_home(env={}, cwd=repo).path == (tmp_path / "outer-home").resolve()


def test_plain_workspace_file_in_place(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "home", "demo")
    found = resolve_home(env={}, cwd=workspace.parent)
    assert found.path == (tmp_path / "home").resolve()
    assert found.source == f"marker:{workspace}"


def test_workspace_file_outside_projects_shape_is_ignored(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "workspace.json").write_text("{}", encoding="utf-8")  # an editor's file, not a marker
    assert find_workspace_marker(repo) is None
    assert resolve_home(env={}, cwd=repo).source == "default"


def test_dangling_and_foreign_symlinks_are_ignored(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "dangling").symlink_to(tmp_path / "missing" / "projects" / "p" / "workspace.json")
    (repo / "elsewhere").symlink_to(tmp_path)
    (repo / "not-a-marker").symlink_to(tmp_path / "repo")
    assert find_workspace_marker(repo) is None


def test_marker_names_the_project(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "home", "demo")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".oacp").symlink_to(workspace)
    assert resolve_home(env={}, cwd=repo).project == "demo"
    assert resolve_home(env={}, cwd=workspace.parent).project == "demo"


not_root = pytest.mark.skipif(os.geteuid() == 0, reason="permission bits ignored as root")


@not_root
@pytest.mark.parametrize("ancestor_binding", [True, False], ids=["with-ancestor-binding", "no-ancestor-binding"])
def test_an_ancestor_that_cannot_be_inspected_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ancestor_binding: bool
) -> None:
    """A directory the process cannot inspect might hold a binding; the walk never passes it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    if ancestor_binding:
        _binding(tmp_path, schema_version=1, home=str(tmp_path / "outer-store"))
    locked = tmp_path / "locked"
    repo = locked / "repo"
    repo.mkdir(parents=True)
    locked.chmod(0)
    if os.access(repo, os.R_OK):
        locked.chmod(0o700)
        pytest.skip("this user traverses directories regardless of their mode bits")
    try:
        with pytest.raises(HomeError, match="cannot inspect the binding slot"):
            resolve_home(env={}, cwd=repo)
    finally:
        locked.chmod(0o700)
