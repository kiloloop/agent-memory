# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""The ``agent-memory`` skill stays true to the CLI it ships with.

The skill lives in this repository and is released with the package, so it
names the release ``__version__`` is headed for, and every command and option it
shows an agent exists. What those commands do is tested with the commands
themselves: test_workflow, test_archive, test_events and test_startup.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_memory import __version__
from agent_memory.cli import build_parser

SKILL = Path(__file__).resolve().parents[1] / "skills" / "agent-memory"
SKILL_FILES = ["SKILL.md", "README.md"]
VERSION_MENTION = re.compile(r"agent-memory(?:-cli==| )(\d+(?:\.\d+)+(?:\.dev\d+)?)")
FENCED = re.compile(r"^```[^\n]*\n(.*?)^```", re.DOTALL | re.MULTILINE)
INLINE = re.compile(r"`([^`]+)`")
COMMAND = re.compile(r"\bagent-memory\s+([a-z][a-z-]*)")
OPTION = re.compile(r"(?<![\w-])--[a-z][a-z-]*")


def _code(text: str) -> list[str]:
    """Fenced blocks and inline code spans: the text an agent runs."""
    return FENCED.findall(text) + INLINE.findall(FENCED.sub("", text))


def _subcommands(parser: argparse.ArgumentParser) -> Iterator[tuple[str, argparse.ArgumentParser]]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            yield from action.choices.items()


def _options(parser: argparse.ArgumentParser) -> set[str]:
    found = set(parser._option_string_actions)
    for _name, command in _subcommands(parser):
        found |= _options(command)
    return found


@pytest.mark.parametrize("name", SKILL_FILES)
def test_the_skill_names_the_release_it_ships_in(name: str) -> None:
    release = re.sub(r"\.dev\d+$", "", __version__)
    named = set(VERSION_MENTION.findall((SKILL / name).read_text(encoding="utf-8")))
    assert named == {release}, (
        f"skills/agent-memory/{name} names {sorted(named)}; this tree releases {release}. "
        "Move the skill's version with __version__."
    )


@pytest.mark.parametrize("name", SKILL_FILES)
def test_the_skill_shows_only_commands_and_options_the_cli_has(name: str) -> None:
    parser = build_parser()
    code = _code((SKILL / name).read_text(encoding="utf-8"))
    commands = {command for span in code for command in COMMAND.findall(span)}
    options = {option for span in code for option in OPTION.findall(span)}

    assert commands and options
    assert commands <= {command for command, _parser in _subcommands(parser)}
    assert options <= _options(parser)
