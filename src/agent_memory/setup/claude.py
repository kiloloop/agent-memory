# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""``agent-memory setup claude``: the Claude Code hook.

Claude Code reads ``.claude/settings.json`` and runs each ``SessionStart``
command with the ``startup`` matcher when a session begins, with the
repository as its working directory; what the command prints to stdout is
added to the session's context. So the script prints its manifest, and a
warning, as plain lines. The registration retires the two hook commands the
kernel used to install, and removes their scripts only when they are byte
for byte what it wrote; the kernel's envelope hook and anything custom stay. Beside the hook goes the
workflow file, a repository skill Claude Code loads on demand.
"""

from __future__ import annotations

from . import legacy
from .common import RuntimeSpec

SETTINGS_SCHEMA = "https://json.schemastore.org/claude-code-settings.json"

SPEC = RuntimeSpec(
    name="claude",
    settings_file=".claude/settings.json",
    script_file=".claude/hooks/agent-memory-pull.sh",
    event="SessionStart",
    matcher="startup",
    timeout=30,
    workflow_file=".claude/skills/agent-memory/SKILL.md",
    report_body="  printf '%s\\n' \"$1\"",
    fresh_settings={"$schema": SETTINGS_SCHEMA},
    legacy_registrations=legacy.CLAUDE_LEGACY_REGISTRATIONS,
    legacy_files=legacy.CLAUDE_LEGACY_FILES,
)
