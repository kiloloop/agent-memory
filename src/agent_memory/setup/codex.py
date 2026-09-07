# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""``agent-memory setup codex``: the Codex hook.

Codex reads ``.codex/hooks.json``; a ``SessionStart`` entry whose matcher is
``^startup$`` runs when a session begins, and the command answers with a
JSON envelope whose ``additionalContext`` the runtime adds to the session.
The script therefore reports through that envelope, and the ``startup`` verb
renders one for this runtime. The entry is the memory half only: it sits
beside the kernel's ``session-init --hook`` entry, which keeps verifying
protocol files and status, and only that entry's ``--pull-memory`` flag is
retired, so memory is pulled once. Its ``additionalContextLimit`` belongs
to it and is never touched. Beside the hook goes the workflow file, a
repository skill under ``.agents/skills/``, where Codex discovers them.
"""

from __future__ import annotations

from . import legacy
from .common import RuntimeSpec

REPORT_BODY = (
    "  printf '{\"continue\": true, \"systemMessage\": \"%s\", \"hookSpecificOutput\": "
    "{\"hookEventName\": \"SessionStart\", \"additionalContext\": \"%s\"}}\\n' \"$1\" \"$1\""
)

SPEC = RuntimeSpec(
    name="codex",
    settings_file=".codex/hooks.json",
    script_file=".codex/hooks/agent-memory-pull.sh",
    event="SessionStart",
    matcher="^startup$",
    timeout=60,
    workflow_file=".agents/skills/agent-memory/SKILL.md",
    report_body=REPORT_BODY,
    hook_fields={"statusMessage": "Pulling agent memory"},
    fresh_settings={"description": "agent-memory startup: pull the memory home and list the files to read."},
    legacy_flag=(legacy.CODEX_SESSION_INIT_PREFIX, legacy.CODEX_LEGACY_PULL_FLAG),
)
