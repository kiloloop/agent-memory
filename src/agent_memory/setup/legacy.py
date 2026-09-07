# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""What earlier tooling installed, so ``setup`` can retire it by exact match.

This is the one module in the package that names the kernel this tool grew
out of: the hook commands its setup registered and the exact scripts it
wrote. A registration is retired when its command equals one of these
strings; a script is removed only when its bytes match one of these
templates, digest for digest. Anything else at those paths is somebody's
own work: it is left alone and named in the report.
"""

from __future__ import annotations

import hashlib
from typing import Dict, Tuple

CLAUDE_LEGACY_PULL_COMMAND = ".claude/hooks/oacp-memory-pull.sh"
CLAUDE_LEGACY_PUSH_COMMAND = ".claude/hooks/oacp-memory-push.sh"

CLAUDE_LEGACY_PULL_SCRIPT = """\
#!/usr/bin/env bash
# Claude hook event: SessionStart (startup)
set -u

OACP_ROOT="${OACP_HOME:-$HOME/oacp}"
if [[ ! -f "$OACP_ROOT/.oacp-memory-repo" ]]; then
  exit 0
fi

oacp memory pull --oacp-dir "$OACP_ROOT" || true
"""

CLAUDE_LEGACY_PUSH_SCRIPT = """\
#!/usr/bin/env bash
# Claude hook event: SessionEnd / wrap-up
set -u

OACP_ROOT="${OACP_HOME:-$HOME/oacp}"
if [[ ! -f "$OACP_ROOT/.oacp-memory-repo" ]]; then
  exit 0
fi

oacp memory push --oacp-dir "$OACP_ROOT" || true
"""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: Registrations retired by exact command, per hook event.
CLAUDE_LEGACY_REGISTRATIONS: Dict[str, Tuple[str, ...]] = {
    "SessionStart": (CLAUDE_LEGACY_PULL_COMMAND,),
    "SessionEnd": (CLAUDE_LEGACY_PUSH_COMMAND,),
}

#: Files removed only when their digest is one of these, per repository-relative path.
CLAUDE_LEGACY_FILES: Dict[str, Tuple[str, ...]] = {
    CLAUDE_LEGACY_PULL_COMMAND: (digest(CLAUDE_LEGACY_PULL_SCRIPT),),
    CLAUDE_LEGACY_PUSH_COMMAND: (digest(CLAUDE_LEGACY_PUSH_SCRIPT),),
}

#: The codex startup command the kernel registers, and the flag that made it pull memory.
#: The entry stays (it verifies protocol files and status); only the flag is retired.
CODEX_SESSION_INIT_PREFIX: Tuple[str, ...] = ("oacp", "session-init", "--hook")
CODEX_LEGACY_PULL_FLAG = "--pull-memory"
