# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""``agent-memory setup <runtime>``: the hook installation grammar, each rule pinned.

The installation matrix (fresh, idempotent, edited managed file, symlinked
target), the fleet settings fixture that carries the legacy push entry, the
generated script run without the tool and against an unreachable remote,
the codex entry beside the kernel's, and the rule that no push hook is
ever installed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import stat
import sys
from pathlib import Path
from typing import Dict, List

import pytest

from agent_memory import __version__, layout, org
from agent_memory.cli import main
from agent_memory.setup import (
    SPECS,
    Result,
    RuntimeSpec,
    SetupError,
    legacy,
    run_setup,
    script_text,
    template_digest,
    workflow_digest,
    workflow_text,
)
from agent_memory.setup.common import (
    CHMOD_SCRIPT,
    CREATE_SETTINGS,
    KEEP_FLAG,
    KEEP_LEGACY_FILE,
    REGENERATE_SCRIPT,
    REGISTER,
    REGISTERED,
    REGISTRATION_HELD,
    REMOVE_LEGACY_FILE,
    RETIRE_REGISTRATION,
    SCRIPT_CONFLICT,
    SCRIPT_IN_PLACE,
    SETTINGS_CONFLICT,
    STRIP_FLAG,
    WORKFLOW_CONFLICT,
    WORKFLOW_IN_PLACE,
    REGENERATE_WORKFLOW,
    WRITE_SCRIPT,
    WRITE_WORKFLOW,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "setup"
CLAUDE = SPECS["claude"]
CODEX = SPECS["codex"]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    org.init(home, project="demo")
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


@pytest.fixture(params=sorted(SPECS), ids=sorted(SPECS))
def spec(request: pytest.FixtureRequest) -> RuntimeSpec:
    return SPECS[request.param]


def _tree(root: Path) -> Dict[str, str]:
    """Every entry under ``root``: symlinks by target, directories as ``rel/``, files by digest and mode."""
    tree: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            tree[rel] = f"link:{os.readlink(path)}"
        elif path.is_dir():
            tree[f"{rel}/"] = "dir"
        else:
            tree[rel] = f"{_sha(path.read_bytes())}:{path.stat().st_mode & 0o777:o}"
    return tree


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _commands(settings: dict, event: str) -> List[str]:
    return [hook["command"] for entry in settings.get("hooks", {}).get(event, []) for hook in entry["hooks"]]


def _actions(result: Result) -> List[str]:
    return [step.action for step in result.plan.steps]


def _place(repo: Path, rel: str, text: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _bash() -> str:
    found = shutil.which("bash")
    if found is None:
        pytest.skip("bash is not available")
    return found


def _tool_on_path(tmp_path: Path) -> Path:
    """A PATH directory whose ``agent-memory`` runs this interpreter's package."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "agent-memory"
    shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m agent_memory "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return bin_dir


def _run_hook(spec: RuntimeSpec, repo: Path, env: Dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([_bash(), spec.script_file], cwd=str(repo), env=env, capture_output=True, text=True, check=False)


# --- the installation matrix ---------------------------------------------------


def test_fresh_install(spec: RuntimeSpec, repo: Path, home: Path) -> None:
    result = run_setup(spec, repo, home)
    assert not result.plan.conflicts
    assert _actions(result) == [WRITE_SCRIPT, CREATE_SETTINGS, REGISTER, WRITE_WORKFLOW]
    script = repo / spec.script_file
    assert script.read_text(encoding="utf-8") == script_text(spec)
    assert script.stat().st_mode & 0o777 == 0o755
    settings = _load(repo / spec.settings_file)
    for key, value in spec.fresh_settings.items():
        assert settings[key] == value
    assert settings["hooks"][spec.event] == [spec.entry()]
    assert result.plan.receipt_path.parent == home / layout.SETUP_DIR / spec.name
    receipt = _load(result.plan.receipt_path)
    assert receipt["managed"][0]["digest"] == template_digest(spec) == _sha(script.read_bytes())
    wrapper = repo / spec.workflow_file
    assert wrapper.read_text(encoding="utf-8") == workflow_text(spec)
    assert receipt["managed"][2]["digest"] == workflow_digest(spec) == _sha(wrapper.read_bytes())


def test_no_push_hook_is_ever_installed(spec: RuntimeSpec, repo: Path, home: Path) -> None:
    assert "push" not in script_text(spec)
    assert f"startup --runtime {spec.name} --pull" in script_text(spec)
    run_setup(spec, repo, home)
    settings = _load(repo / spec.settings_file)
    assert "SessionEnd" not in settings["hooks"]
    assert [path for path in _tree(repo) if "push" in path] == []


def test_rerun_changes_no_byte(spec: RuntimeSpec, repo: Path, home: Path) -> None:
    run_setup(spec, repo, home)
    before = (_tree(repo), _tree(home))
    again = run_setup(spec, repo, home)
    assert not again.plan.changed
    assert _actions(again) == [SCRIPT_IN_PLACE, REGISTERED, WORKFLOW_IN_PLACE]
    assert (_tree(repo), _tree(home)) == before


def test_dry_run_writes_nothing_and_plans_the_same_steps(spec: RuntimeSpec, repo: Path, home: Path) -> None:
    home_before = _tree(home)
    dry = run_setup(spec, repo, home, dry_run=True)
    assert dry.plan.changed and dry.plan.receipt_write
    assert _tree(repo) == {".git/": "dir"}
    assert _tree(home) == home_before
    real = run_setup(spec, repo, home)
    assert [(step.action, step.path) for step in dry.plan.steps] == [(step.action, step.path) for step in real.plan.steps]


def test_edited_script_is_a_named_conflict_and_kept(spec: RuntimeSpec, repo: Path, home: Path) -> None:
    run_setup(spec, repo, home)
    script = repo / spec.script_file
    edited = script_text(spec) + "echo mine\n"
    script.write_text(edited, encoding="utf-8")
    result = run_setup(spec, repo, home)
    assert _actions(result) == [SCRIPT_CONFLICT, REGISTERED, WORKFLOW_IN_PLACE]
    assert [step.path for step in result.plan.conflicts] == [spec.script_file]
    assert "edited" in result.plan.conflicts[0].detail
    assert script.read_text(encoding="utf-8") == edited
    receipt = _load(result.plan.receipt_path)
    assert receipt["managed"][0]["state"] == "conflict"
    assert receipt["managed"][0]["digest"] == _sha(edited.encode("utf-8"))
    assert receipt["conflicts"] == [{"path": spec.script_file, "detail": result.plan.conflicts[0].detail}]
    assert main(["setup", spec.name, "--repo", str(repo), "--home", str(home)]) == 3


def test_an_earlier_template_is_regenerated(spec: RuntimeSpec, repo: Path, home: Path) -> None:
    old = "#!/usr/bin/env bash\n# an earlier shipped template\n"
    _place(repo, spec.script_file, old)
    older = dataclasses.replace(spec, previous_template_digests=(_sha(old.encode("utf-8")),))
    result = run_setup(older, repo, home)
    assert _actions(result) == [REGENERATE_SCRIPT, CREATE_SETTINGS, REGISTER, WRITE_WORKFLOW]
    script = repo / spec.script_file
    assert script.read_text(encoding="utf-8") == script_text(spec)
    assert script.stat().st_mode & 0o777 == 0o755


def test_symlinked_script_is_never_written_through(spec: RuntimeSpec, repo: Path, home: Path, tmp_path: Path) -> None:
    shared = tmp_path / "shared" / "hook.sh"
    shared.parent.mkdir()
    shared.write_text(script_text(spec), encoding="utf-8")
    shared.chmod(0o755)
    script = repo / spec.script_file
    script.parent.mkdir(parents=True)
    script.symlink_to(shared)

    result = run_setup(spec, repo, home)
    assert _actions(result) == [SCRIPT_IN_PLACE, CREATE_SETTINGS, REGISTER, WRITE_WORKFLOW]
    assert "symlink" in result.plan.steps[0].detail
    receipt = _load(result.plan.receipt_path)
    assert receipt["managed"][0]["symlink"] is True
    assert receipt["managed"][0]["resolved"] == os.path.realpath(shared)

    shared.write_text("# somebody else's hook\n", encoding="utf-8")
    result = run_setup(spec, repo, home)
    assert _actions(result) == [SCRIPT_CONFLICT, REGISTERED, WORKFLOW_IN_PLACE]
    assert os.path.realpath(shared) in result.plan.conflicts[0].detail
    assert shared.read_text(encoding="utf-8") == "# somebody else's hook\n"
    assert script.is_symlink()


def test_dangling_script_symlink_holds_the_registration(spec: RuntimeSpec, repo: Path, home: Path, tmp_path: Path) -> None:
    script = repo / spec.script_file
    script.parent.mkdir(parents=True)
    script.symlink_to(tmp_path / "missing.sh")
    result = run_setup(spec, repo, home)
    assert _actions(result) == [SCRIPT_CONFLICT, CREATE_SETTINGS, REGISTRATION_HELD, WRITE_WORKFLOW]
    assert not (tmp_path / "missing.sh").exists()
    assert _commands(_load(repo / spec.settings_file), spec.event) == []
    assert _load(result.plan.receipt_path)["managed"][1]["state"] == "held"
    assert main(["setup", spec.name, "--repo", str(repo), "--home", str(home)]) == 3


def test_symlinked_hooks_directory_is_protected(spec: RuntimeSpec, repo: Path, home: Path, tmp_path: Path) -> None:
    shared_dir = tmp_path / "shared-hooks"
    shared_dir.mkdir()
    hooks_dir = (repo / spec.script_file).parent
    hooks_dir.parent.mkdir(parents=True, exist_ok=True)
    hooks_dir.symlink_to(shared_dir)
    result = run_setup(spec, repo, home)
    assert result.plan.steps[0].action == SCRIPT_CONFLICT
    assert os.path.realpath(shared_dir) in result.plan.steps[0].detail
    assert list(shared_dir.iterdir()) == []
    assert REGISTRATION_HELD in _actions(result)


def test_symlinked_settings_file_is_not_edited(spec: RuntimeSpec, repo: Path, home: Path, tmp_path: Path) -> None:
    shared = tmp_path / "shared-settings.json"
    shared.write_text("{}\n", encoding="utf-8")
    settings = repo / spec.settings_file
    settings.parent.mkdir(parents=True)
    settings.symlink_to(shared)
    result = run_setup(spec, repo, home)
    assert _actions(result) == [WRITE_SCRIPT, SETTINGS_CONFLICT, WRITE_WORKFLOW]
    assert os.path.realpath(shared) in result.plan.steps[1].detail
    assert shared.read_text(encoding="utf-8") == "{}\n"
    assert (repo / spec.script_file).is_file()
    receipt = _load(result.plan.receipt_path)
    assert receipt["managed"][1]["symlink"] is True and receipt["managed"][1]["state"] == "conflict"
    assert receipt["managed"][0]["state"] == "installed"
    assert main(["setup", spec.name, "--repo", str(repo), "--home", str(home)]) == 3


@pytest.mark.parametrize("content", ["{", "[]", '{"hooks": "x"}', '{"hooks": {"SessionStart": "x"}}'])
def test_unusable_settings_file_is_a_conflict_not_a_crash(spec: RuntimeSpec, repo: Path, home: Path, content: str) -> None:
    settings = _place(repo, spec.settings_file, content)
    result = run_setup(spec, repo, home)
    assert _actions(result) == [WRITE_SCRIPT, SETTINGS_CONFLICT, WRITE_WORKFLOW]
    assert settings.read_text(encoding="utf-8") == content


def test_missing_home_is_an_error_naming_init(spec: RuntimeSpec, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SetupError, match="agent-memory init"):
        run_setup(spec, repo, tmp_path / "nope")
    assert main(["setup", spec.name, "--repo", str(repo), "--home", str(tmp_path / "nope")]) == 1
    assert "agent-memory init" in capsys.readouterr().err
    assert _tree(repo) == {".git/": "dir"}


def test_missing_repo_is_an_error(spec: RuntimeSpec, home: Path, tmp_path: Path) -> None:
    with pytest.raises(SetupError, match="not a directory"):
        run_setup(spec, tmp_path / "nope", home)


# --- resuming and never duplicating ---------------------------------------------


def test_resumes_after_an_interruption(spec: RuntimeSpec, repo: Path, home: Path) -> None:
    _place(repo, spec.script_file, script_text(spec))  # interrupted after the script was written
    result = run_setup(spec, repo, home)
    assert _actions(result) == [SCRIPT_IN_PLACE, CREATE_SETTINGS, REGISTER, WRITE_WORKFLOW]
    result.plan.receipt_path.unlink()  # the receipt lost: rewritten, the repository untouched
    before = _tree(repo)
    again = run_setup(spec, repo, home)
    assert again.plan.receipt_write and again.plan.receipt_path.is_file()
    assert _actions(again) == [SCRIPT_IN_PLACE, REGISTERED, WORKFLOW_IN_PLACE]
    assert _tree(repo) == before


def test_registration_without_a_script_gets_the_script(spec: RuntimeSpec, repo: Path, home: Path) -> None:
    settings = _place(repo, spec.settings_file, json.dumps({"hooks": {spec.event: [spec.entry()]}}))
    result = run_setup(spec, repo, home)
    assert _actions(result) == [WRITE_SCRIPT, REGISTERED, WRITE_WORKFLOW]
    assert _load(settings)["hooks"][spec.event] == [spec.entry()]


def test_registration_inside_a_custom_entry_is_not_duplicated_or_edited(spec: RuntimeSpec, repo: Path, home: Path) -> None:
    custom = {
        "matcher": "startup|resume",
        "hooks": [
            {"type": "command", "command": "./mine.sh", "timeout": 5},
            {"type": "command", "command": spec.script_file, "timeout": 99},
        ],
    }
    original = json.dumps({"hooks": {spec.event: [custom]}}, indent=4)
    settings = _place(repo, spec.settings_file, original)
    result = run_setup(spec, repo, home)
    assert _actions(result) == [WRITE_SCRIPT, REGISTERED, WRITE_WORKFLOW]
    assert settings.read_text(encoding="utf-8") == original


# --- the fleet fixture: legacy hooks retired, custom entries kept ---------------


def _fleet_repo(repo: Path) -> Path:
    settings = repo / CLAUDE.settings_file
    settings.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / "claude_settings_fleet.json", settings)
    _place(repo, legacy.CLAUDE_LEGACY_PULL_COMMAND, legacy.CLAUDE_LEGACY_PULL_SCRIPT)
    _place(repo, legacy.CLAUDE_LEGACY_PUSH_COMMAND, legacy.CLAUDE_LEGACY_PUSH_SCRIPT)
    return settings


def test_legacy_digests_pin_the_fleet_templates() -> None:
    # Measured on the fleet's eleven pull and seven push hook files, 2026-09-06: one digest each.
    assert legacy.CLAUDE_LEGACY_FILES == {
        ".claude/hooks/oacp-memory-pull.sh": ("3284e8c17f644bc166fbe3e5a617ec66ecf2028ed58b8f2eee6aaf11357c79f3",),
        ".claude/hooks/oacp-memory-push.sh": ("bb2c02b7f529b18e9aacbe1162a9082b36a713dd1b09e4ebdc6223ca9ab863cc",),
    }


def test_fleet_settings_retire_the_legacy_hooks_and_keep_custom_entries(repo: Path, home: Path) -> None:
    settings = _fleet_repo(repo)
    fixture = _load(FIXTURES / "claude_settings_fleet.json")
    result = run_setup(CLAUDE, repo, home)
    assert not result.plan.conflicts
    assert _actions(result) == [
        WRITE_SCRIPT,
        RETIRE_REGISTRATION,
        RETIRE_REGISTRATION,
        REGISTER,
        REMOVE_LEGACY_FILE,
        REMOVE_LEGACY_FILE,
        WRITE_WORKFLOW,
    ]
    after = _load(settings)
    assert "SessionEnd" not in after["hooks"]
    assert after["hooks"]["PreToolUse"] == fixture["hooks"]["PreToolUse"]
    assert after["$schema"] == fixture["$schema"]
    assert _commands(after, "SessionStart") == [CLAUDE.script_file]
    assert not (repo / legacy.CLAUDE_LEGACY_PULL_COMMAND).exists()
    assert not (repo / legacy.CLAUDE_LEGACY_PUSH_COMMAND).exists()
    receipt = _load(result.plan.receipt_path)
    assert [(item["kind"], item.get("removed")) for item in receipt["retired"]] == [
        ("registration", None),
        ("registration", None),
        ("file", True),
        ("file", True),
    ]
    again = run_setup(CLAUDE, repo, home)
    assert not again.plan.changed
    assert _load(again.plan.receipt_path)["retired"] == receipt["retired"]  # the history stays recorded


def test_edited_legacy_script_is_kept_while_its_registration_is_retired(repo: Path, home: Path) -> None:
    settings = _fleet_repo(repo)
    push = repo / legacy.CLAUDE_LEGACY_PUSH_COMMAND
    push.write_text(legacy.CLAUDE_LEGACY_PUSH_SCRIPT + "echo custom\n", encoding="utf-8")
    result = run_setup(CLAUDE, repo, home)
    assert not result.plan.conflicts
    kept = [step for step in result.plan.steps if step.action == KEEP_LEGACY_FILE]
    assert [step.path for step in kept] == [legacy.CLAUDE_LEGACY_PUSH_COMMAND]
    assert "edited" in kept[0].detail
    assert "custom" in push.read_text(encoding="utf-8")
    assert "SessionEnd" not in _load(settings)["hooks"]
    assert not (repo / legacy.CLAUDE_LEGACY_PULL_COMMAND).exists()


# The legacy-file preservation matrix: every uncertainty keeps the file. Each row plants
# the shipped legacy pull template and one doubt; ``detail`` is the reason the report names.
PULL = legacy.CLAUDE_LEGACY_PULL_COMMAND
PULL_SCRIPT = legacy.CLAUDE_LEGACY_PULL_SCRIPT
PRESERVATION_ROWS = {
    "leaf_symlink": "symlink",
    "linked_hooks_dir": ".claude/hooks is a symlink",
    "settings_symlink": "could not be read in full",
    "settings_not_json": "could not be read in full",
    "settings_not_an_object": "could not be read in full",
    "custom_wrapper": "still named by `bash .claude/hooks/oacp-memory-pull.sh`",
    "custom_wrapper_by_basename": "still named by `cd .claude/hooks && ./oacp-memory-pull.sh`",
    "custom_wrapper_other_event": "still named by `bash .claude/hooks/oacp-memory-pull.sh`",
    "edited": "edited, kept",
}


def _plant(repo: Path, row: str, tmp_path: Path) -> Path:
    """The repository of one matrix row; returns the legacy file's path (through any link)."""
    settings = repo / CLAUDE.settings_file
    hooks_dir = repo / ".claude" / "hooks"
    entry = {"matcher": "startup", "hooks": [{"type": "command", "command": PULL}]}
    data: object = {"hooks": {"SessionStart": [entry]}}
    if row == "linked_hooks_dir":
        shared = tmp_path / "shared-hooks"
        shared.mkdir()
        hooks_dir.parent.mkdir(parents=True)
        hooks_dir.symlink_to(shared, target_is_directory=True)
    else:
        hooks_dir.mkdir(parents=True)
    legacy_file = repo / PULL
    if row == "leaf_symlink":
        shared_file = tmp_path / "shared-pull.sh"
        shared_file.write_text(PULL_SCRIPT, encoding="utf-8")
        legacy_file.symlink_to(shared_file)
    else:
        legacy_file.write_text(PULL_SCRIPT + ("echo custom\n" if row == "edited" else ""), encoding="utf-8")
        legacy_file.chmod(0o755)
    if row == "custom_wrapper":
        entry["hooks"][0]["command"] = f"bash {PULL}"
    elif row == "custom_wrapper_by_basename":
        entry["hooks"][0]["command"] = "cd .claude/hooks && ./oacp-memory-pull.sh"
    elif row == "custom_wrapper_other_event":
        data = {"hooks": {"PreCompact": [{"hooks": [{"type": "command", "command": f"bash {PULL}"}]}]}}
    text = json.dumps(data, indent=2)
    if row == "settings_not_json":
        text = "{broken"
    elif row == "settings_not_an_object":
        text = "[]"
    if row == "settings_symlink":
        shared_settings = tmp_path / "shared-settings.json"
        shared_settings.write_text(text, encoding="utf-8")
        settings.symlink_to(shared_settings)
    else:
        settings.write_text(text, encoding="utf-8")
    return legacy_file


@pytest.mark.parametrize("row", sorted(PRESERVATION_ROWS))
def test_legacy_file_preservation_matrix(repo: Path, home: Path, tmp_path: Path, row: str) -> None:
    legacy_file = _plant(repo, row, tmp_path)
    settings = repo / CLAUDE.settings_file
    settings_before = settings.read_bytes()
    dry = run_setup(CLAUDE, repo, home, dry_run=True)
    result = run_setup(CLAUDE, repo, home)
    assert _actions(dry) == _actions(result)
    kept = [step for step in result.plan.steps if step.action == KEEP_LEGACY_FILE]
    assert [step.path for step in kept] == [PULL], _actions(result)
    assert PRESERVATION_ROWS[row] in kept[0].detail, kept[0].detail
    assert REMOVE_LEGACY_FILE not in _actions(result) and not result.plan.removals
    assert os.path.lexists(legacy_file)
    assert _sha(legacy_file.read_bytes()) == _sha((PULL_SCRIPT + ("echo custom\n" if row == "edited" else "")).encode())
    custom = row.startswith("custom_wrapper") or row.startswith("settings") or row == "linked_hooks_dir"
    if custom:
        # A command this tool does not recognize, or settings it cannot read in full, are never edited.
        assert RETIRE_REGISTRATION not in _actions(result)
    if row.startswith("settings") or row == "linked_hooks_dir":
        assert settings.read_bytes() == settings_before
    if row.startswith("custom_wrapper"):
        after = _load(settings)
        event = "PreCompact" if row == "custom_wrapper_other_event" else "SessionStart"
        assert _commands(after, event)[0] in {f"bash {PULL}", "cd .claude/hooks && ./oacp-memory-pull.sh"}


def test_held_registration_leaves_the_legacy_hooks_in_place(repo: Path, home: Path, tmp_path: Path) -> None:
    """When the new script cannot be placed, nothing is retired: a repository is never left without a memory hook."""
    _plant(repo, "linked_hooks_dir", tmp_path)
    settings = repo / CLAUDE.settings_file
    before = settings.read_bytes()
    result = run_setup(CLAUDE, repo, home)
    assert _actions(result) == [SCRIPT_CONFLICT, REGISTRATION_HELD, KEEP_LEGACY_FILE, WRITE_WORKFLOW]
    assert "legacy hooks stay" in result.plan.steps[1].detail
    assert settings.read_bytes() == before
    assert _commands(_load(settings), "SessionStart") == [PULL]


def test_legacy_script_still_registered_elsewhere_is_kept(repo: Path, home: Path) -> None:
    settings = _fleet_repo(repo)
    data = _load(settings)
    data["hooks"]["PreCompact"] = [{"hooks": [{"type": "command", "command": legacy.CLAUDE_LEGACY_PULL_COMMAND}]}]
    settings.write_text(json.dumps(data, indent=2), encoding="utf-8")
    result = run_setup(CLAUDE, repo, home)
    kept = next(step for step in result.plan.steps if step.action == KEEP_LEGACY_FILE)
    assert kept.path == legacy.CLAUDE_LEGACY_PULL_COMMAND and "still registered" in kept.detail
    assert (repo / legacy.CLAUDE_LEGACY_PULL_COMMAND).is_file()
    assert _commands(_load(settings), "PreCompact") == [legacy.CLAUDE_LEGACY_PULL_COMMAND]
    assert not (repo / legacy.CLAUDE_LEGACY_PUSH_COMMAND).exists()


# --- codex: beside the kernel's entry ------------------------------------------


def test_codex_entry_sits_beside_the_kernel_entry_and_retires_only_its_pull_flag(repo: Path, home: Path) -> None:
    hooks_file = repo / CODEX.settings_file
    hooks_file.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / "codex_hooks_kernel.json", hooks_file)
    fixture = _load(FIXTURES / "codex_hooks_kernel.json")
    result = run_setup(CODEX, repo, home)
    assert not result.plan.conflicts
    assert _actions(result) == [WRITE_SCRIPT, STRIP_FLAG, REGISTER, WRITE_WORKFLOW]
    after = _load(hooks_file)
    assert after["description"] == fixture["description"]
    kernel, custom, ours = after["hooks"]["SessionStart"]
    kernel_hook = kernel["hooks"][0]
    fixture_hook = fixture["hooks"]["SessionStart"][0]["hooks"][0]
    assert kernel_hook["command"] == "oacp session-init --hook --project demo --hub-dir /home/user/oacp"
    assert kernel_hook["additionalContextLimit"] == 4000
    assert {k: v for k, v in kernel_hook.items() if k != "command"} == {k: v for k, v in fixture_hook.items() if k != "command"}
    assert custom == fixture["hooks"]["SessionStart"][1]
    assert ours == CODEX.entry()
    assert ours["matcher"] == "^startup$" and ours["hooks"][0]["statusMessage"] == "Pulling agent memory"
    assert "additionalContextLimit" not in ours["hooks"][0]
    again = run_setup(CODEX, repo, home)
    assert not again.plan.changed and _actions(again) == [SCRIPT_IN_PLACE, REGISTERED, WORKFLOW_IN_PLACE]


def _codex_repo_with(repo: Path, command: str) -> Path:
    hooks_file = repo / CODEX.settings_file
    hooks_file.parent.mkdir(parents=True)
    data = {"hooks": {"SessionStart": [{"matcher": "^startup$", "hooks": [{"type": "command", "command": command}]}]}}
    hooks_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return hooks_file


# The flag-retirement grammar: one simple command is edited in place, byte for byte around the flag;
# anything the shell reads as more than that is left exactly as written and named.
FLAG_ROWS = {
    "plain": ("oacp session-init --hook --pull-memory --project demo", "oacp session-init --hook --project demo"),
    "last": ("oacp session-init --hook --project demo --pull-memory", "oacp session-init --hook --project demo"),
    "quoted_value_kept": ('oacp session-init --hook --hub-dir "$HOME/oacp" --pull-memory', 'oacp session-init --hook --hub-dir "$HOME/oacp"'),
    "twice": ("oacp session-init --hook --pull-memory --pull-memory", "oacp session-init --hook"),
    "and_list": ("oacp session-init --hook --project demo --pull-memory && echo CUSTOM", None),
    "or_list": ("oacp session-init --hook --pull-memory || true", None),
    "pipe": ("oacp session-init --hook --pull-memory | tee log", None),
    "sequence": ("oacp session-init --hook --pull-memory; true", None),
    "redirect": ("oacp session-init --hook --pull-memory > /dev/null", None),
    "substitution": ("oacp session-init --hook --hub-dir $(pwd) --pull-memory", None),
    "backticks": ("oacp session-init --hook --hub-dir `pwd` --pull-memory", None),
    "quoted_flag": ("oacp session-init --hook '--pull-memory'", None),
    # A newline separates commands as `;` does; the flag on a later line belongs to that line's command.
    "newline_separated": ("oacp session-init --hook\nprintf '%s\\n' --pull-memory", None),
    "flag_on_both_lines": ("oacp session-init --hook --pull-memory\nprintf '%s\\n' --pull-memory", None),
    "crlf_separated": ("oacp session-init --hook --pull-memory\r\necho CUSTOM", None),
}


@pytest.mark.parametrize("row", sorted(FLAG_ROWS))
def test_codex_pull_flag_is_retired_only_from_one_simple_command(repo: Path, home: Path, row: str) -> None:
    before, expected = FLAG_ROWS[row]
    hooks_file = _codex_repo_with(repo, before)
    result = run_setup(CODEX, repo, home)
    assert not result.plan.conflicts
    kernel_after = _commands(_load(hooks_file), "SessionStart")[0]
    if expected is None:
        assert _actions(result) == [WRITE_SCRIPT, KEEP_FLAG, REGISTER, WRITE_WORKFLOW]
        assert kernel_after == before
        assert f"left in `{before}`" in result.plan.steps[1].detail
    else:
        assert _actions(result) == [WRITE_SCRIPT, STRIP_FLAG, REGISTER, WRITE_WORKFLOW]
        assert kernel_after == expected
    assert "--pull-memory" not in kernel_after or expected is None
    again = run_setup(CODEX, repo, home)
    assert not again.plan.changed


def test_codex_fresh_hooks_file(repo: Path, home: Path) -> None:
    run_setup(CODEX, repo, home)
    assert _load(repo / CODEX.settings_file) == {
        "description": CODEX.fresh_settings["description"],
        "hooks": {"SessionStart": [CODEX.entry()]},
    }
    assert CODEX.entry()["hooks"][0]["timeout"] == 60


# --- the execute bit -------------------------------------------------------------


def test_template_without_its_execute_bit_is_repaired_and_runs(spec: RuntimeSpec, repo: Path, home: Path) -> None:
    script = _place(repo, spec.script_file, script_text(spec))
    script.chmod(0o644)
    dry = run_setup(spec, repo, home, dry_run=True)
    assert _actions(dry) == [CHMOD_SCRIPT, CREATE_SETTINGS, REGISTER, WRITE_WORKFLOW] and script.stat().st_mode & 0o777 == 0o644
    result = run_setup(spec, repo, home)
    assert _actions(result) == [CHMOD_SCRIPT, CREATE_SETTINGS, REGISTER, WRITE_WORKFLOW] and not result.plan.conflicts
    assert "0644" in result.plan.steps[0].detail
    assert script.stat().st_mode & 0o777 == 0o755
    assert script.read_text(encoding="utf-8") == script_text(spec)
    managed = _load(result.plan.receipt_path)["managed"][0]
    assert managed["state"] == "installed" and managed["mode"] == "0755"
    run = subprocess.run(["/bin/sh", "-c", spec.script_file], cwd=str(repo), capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    again = run_setup(spec, repo, home)
    assert not again.plan.changed and _actions(again) == [SCRIPT_IN_PLACE, REGISTERED, WORKFLOW_IN_PLACE]


def test_linked_template_without_its_execute_bit_is_held_not_chmodded(spec: RuntimeSpec, repo: Path, home: Path, tmp_path: Path) -> None:
    shared = tmp_path / "shared.sh"
    shared.write_text(script_text(spec), encoding="utf-8")
    shared.chmod(0o644)
    script = repo / spec.script_file
    script.parent.mkdir(parents=True)
    script.symlink_to(shared)
    result = run_setup(spec, repo, home)
    assert _actions(result) == [SCRIPT_CONFLICT, CREATE_SETTINGS, REGISTRATION_HELD, WRITE_WORKFLOW]
    assert "not executable (mode 0644)" in result.plan.steps[0].detail
    assert shared.stat().st_mode & 0o777 == 0o644 and script.is_symlink()
    assert _commands(_load(repo / spec.settings_file), spec.event) == []
    managed = _load(result.plan.receipt_path)["managed"][0]
    assert managed == {
        "path": spec.script_file,
        "resolved": os.path.realpath(shared),
        "symlink": True,
        "state": "conflict",
        "digest": template_digest(spec),
        "template_digest": template_digest(spec),
        "mode": "0644",
    }


def test_owner_executable_template_is_left_at_its_mode(spec: RuntimeSpec, repo: Path, home: Path) -> None:
    script = _place(repo, spec.script_file, script_text(spec))
    script.chmod(0o700)
    result = run_setup(spec, repo, home)
    assert _actions(result) == [SCRIPT_IN_PLACE, CREATE_SETTINGS, REGISTER, WRITE_WORKFLOW]
    assert script.stat().st_mode & 0o777 == 0o700
    assert _load(result.plan.receipt_path)["managed"][0]["mode"] == "0700"


# --- the generated script, run ------------------------------------------------


def test_generated_script_without_the_tool_warns_and_exits_zero(spec: RuntimeSpec, repo: Path, home: Path, tmp_path: Path) -> None:
    run_setup(spec, repo, home)
    empty = tmp_path / "empty-path"
    empty.mkdir()
    completed = _run_hook(spec, repo, {"PATH": str(empty), "HOME": str(tmp_path)})
    assert completed.returncode == 0, completed.stderr
    assert "command not found" in completed.stdout
    if spec.name == "codex":
        payload = json.loads(completed.stdout)
        assert payload["continue"] is True
        assert "command not found" in payload["hookSpecificOutput"]["additionalContext"]


def test_generated_script_warns_and_exits_zero_when_the_remote_is_unreachable(
    spec: RuntimeSpec, repo: Path, home: Path, tmp_path: Path, git_env: None
) -> None:
    run_setup(spec, repo, home)
    assert main(["enable", "--home", str(home), "--remote", str(tmp_path / "missing.git")]) == 1
    assert (home / layout.MARKER_FILE).is_file()
    env = {**os.environ, "PATH": os.pathsep.join([str(_tool_on_path(tmp_path)), os.environ.get("PATH", "")])}
    env["AGENT_MEMORY_HOME"] = str(home)
    completed = _run_hook(spec, repo, env)
    assert completed.returncode == 0, completed.stderr
    assert "Traceback" not in completed.stderr
    out = completed.stdout
    if spec.name == "codex":
        payload = json.loads(out)
        assert payload["continue"] is True and "degraded" in payload["systemMessage"]
        out = payload["hookSpecificOutput"]["additionalContext"]
    assert "memory pull" in out and "stale" in out


def test_generated_script_pulls_and_prints_the_manifest(spec: RuntimeSpec, repo: Path, home: Path, tmp_path: Path, git_env: None) -> None:
    run_setup(spec, repo, home)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    assert main(["enable", "--home", str(home), "--remote", str(remote)]) == 0
    (repo / ".agent-memory.json").write_text(
        json.dumps({"schema_version": 1, "project": "demo", "home": str(home)}), encoding="utf-8"
    )
    env = {**os.environ, "PATH": os.pathsep.join([str(_tool_on_path(tmp_path)), os.environ.get("PATH", "")])}
    env.pop("AGENT_MEMORY_HOME", None)
    env.pop("OACP_HOME", None)
    completed = _run_hook(spec, repo, env)
    assert completed.returncode == 0, completed.stderr
    out = completed.stdout
    if spec.name == "codex":
        payload = json.loads(out)
        assert payload["continue"] is True and "systemMessage" not in payload
        out = payload["hookSpecificOutput"]["additionalContext"]
    assert "memory pull: already synced." in out
    assert "project demo (binding:" in out
    assert "1. projects/demo/memory/project_facts.md: readable" in out
    assert f"agent-memory startup ({spec.name})" in out


# --- the command line and the receipt --------------------------------------------


def test_cli_setup_reports_lines_json_and_dry_run(spec: RuntimeSpec, repo: Path, home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["setup", spec.name, "--repo", str(repo), "--home", str(home), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"+ {spec.script_file}: would write" in out and "dry run: nothing was written." in out
    assert _tree(repo) == {".git/": "dir"}
    assert main(["setup", spec.name, "--repo", str(repo), "--home", str(home), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1 and payload["action"] == "setup" and payload["runtime"] == spec.name
    assert payload["changed"] is True and payload["conflicts"] == [] and payload["receipt"]["written"] is True
    assert [step["action"] for step in payload["steps"]] == [WRITE_SCRIPT, CREATE_SETTINGS, REGISTER, WRITE_WORKFLOW]
    assert main(["setup", spec.name, "--repo", str(repo), "--home", str(home)]) == 0
    assert "nothing to do" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exit_info:
        main(["setup", "vim"])
    assert exit_info.value.code == 2


def test_cli_setup_finds_the_repo_from_a_subdirectory(spec: RuntimeSpec, repo: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert main(["setup", spec.name, "--home", str(home)]) == 0
    assert (repo / spec.script_file).is_file()
    assert not (sub / spec.script_file).exists()


def test_receipt_records_what_was_installed(spec: RuntimeSpec, repo: Path, home: Path) -> None:
    result = run_setup(spec, repo, home)
    receipt = _load(result.plan.receipt_path)
    assert receipt["schema_version"] == 1 and receipt["tool"] == "agent-memory" and receipt["version"] == __version__
    assert receipt["runtime"] == spec.name and receipt["repo"] == str(repo) and receipt["home"] == str(home)
    assert receipt["written_at_utc"].endswith("Z")
    script, settings, flow = receipt["managed"]
    assert script == {
        "path": spec.script_file,
        "resolved": os.path.realpath(repo / spec.script_file),
        "symlink": False,
        "state": "installed",
        "digest": template_digest(spec),
        "template_digest": template_digest(spec),
        "mode": "0755",
    }
    assert settings["path"] == spec.settings_file and settings["state"] == "registered"
    assert settings["digest"] == _sha((repo / spec.settings_file).read_bytes())
    assert settings["registration"] == {"event": spec.event, "matcher": spec.matcher, "command": spec.script_file}
    assert flow == {
        "path": spec.workflow_file,
        "resolved": os.path.realpath(repo / spec.workflow_file),
        "symlink": False,
        "state": "installed",
        "digest": workflow_digest(spec),
        "template_digest": workflow_digest(spec),
    }
    assert receipt["retired"] == [] and receipt["conflicts"] == []


def test_setup_receipts_never_sync(tmp_path: Path, git_env: None) -> None:
    assert not layout.is_allowed_memory_path(f"{layout.SETUP_DIR}/claude/abc.json")
    home = tmp_path / "home"
    layout.scaffold_home(home)
    receipt = home / layout.SETUP_DIR / "claude" / "abc.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet"], cwd=str(home), check=True)
    ignored = subprocess.run(["git", "check-ignore", "-q", f"{layout.SETUP_DIR}/claude/abc.json"], cwd=str(home), check=False)
    assert ignored.returncode == 0


# --- the workflow file beside the hook (AM-07) -----------------------------------


def test_workflow_paths_are_the_runtimes_repository_skill_files() -> None:
    assert CLAUDE.workflow_file == ".claude/skills/agent-memory/SKILL.md"
    assert CODEX.workflow_file == ".agents/skills/agent-memory/SKILL.md"


def test_workflow_file_is_the_shipped_text_with_the_runtime_name(spec: RuntimeSpec, repo: Path, home: Path) -> None:
    run_setup(spec, repo, home)
    wrapper = repo / spec.workflow_file
    text = wrapper.read_text(encoding="utf-8")
    assert text == workflow_text(spec) and f"workflow for {spec.name}" in text and f"--agent {spec.name}" in text
    assert text.startswith("---\nname: agent-memory\n")
    assert not wrapper.stat().st_mode & stat.S_IXUSR


def test_edited_workflow_file_is_a_named_conflict_and_kept(spec: RuntimeSpec, repo: Path, home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_setup(spec, repo, home)
    wrapper = repo / spec.workflow_file
    wrapper.write_text("# mine\n", encoding="utf-8")
    again = run_setup(spec, repo, home)
    assert [step.action for step in again.plan.conflicts] == [WORKFLOW_CONFLICT]
    assert "edited, kept" in again.plan.conflicts[0].detail
    assert wrapper.read_text(encoding="utf-8") == "# mine\n"
    assert _load(again.plan.receipt_path)["managed"][2]["state"] == "conflict"
    assert main(["setup", spec.name, "--home", str(home), "--repo", str(repo)]) == 3
    assert f"! {spec.workflow_file}: conflict, kept" in capsys.readouterr().out


def test_earlier_workflow_template_is_regenerated(spec: RuntimeSpec, repo: Path, home: Path) -> None:
    run_setup(spec, repo, home)
    wrapper = repo / spec.workflow_file
    wrapper.write_text("# earlier\n", encoding="utf-8")
    earlier = dataclasses.replace(spec, previous_workflow_digests=(_sha(b"# earlier\n"),))
    again = run_setup(earlier, repo, home)
    assert REGENERATE_WORKFLOW in _actions(again) and not again.plan.conflicts
    assert wrapper.read_text(encoding="utf-8") == workflow_text(spec)


def test_symlinked_workflow_file_is_never_written_through(spec: RuntimeSpec, repo: Path, home: Path, tmp_path: Path) -> None:
    shared = tmp_path / "shared-skill.md"
    shared.write_text("# shared\n", encoding="utf-8")
    wrapper = repo / spec.workflow_file
    wrapper.parent.mkdir(parents=True)
    wrapper.symlink_to(shared)
    result = run_setup(spec, repo, home)
    assert [step.action for step in result.plan.conflicts] == [WORKFLOW_CONFLICT]
    assert "is a symlink to" in result.plan.conflicts[0].detail
    assert shared.read_text(encoding="utf-8") == "# shared\n" and wrapper.is_symlink()
    assert _load(result.plan.receipt_path)["managed"][2]["symlink"] is True


def test_setup_accepts_a_repository_without_git(spec: RuntimeSpec, home: Path, tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    result = run_setup(spec, scratch, home)
    assert not result.plan.conflicts
    assert (scratch / spec.script_file).is_file() and (scratch / spec.workflow_file).is_file()
