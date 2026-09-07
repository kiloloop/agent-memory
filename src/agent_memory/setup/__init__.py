# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""``agent-memory setup <runtime>``: one planner, one applier, a spec per runtime."""

from __future__ import annotations

from typing import Dict

from . import claude, codex
from .common import (
    Plan,
    Result,
    RuntimeSpec,
    SetupError,
    Step,
    apply_plan,
    detect_repo,
    known_template_digests,
    known_workflow_digests,
    lines,
    plan_setup,
    run_setup,
    script_text,
    template_digest,
    to_json,
    workflow_digest,
    workflow_text,
)

SPECS: Dict[str, RuntimeSpec] = {claude.SPEC.name: claude.SPEC, codex.SPEC.name: codex.SPEC}
RUNTIMES = tuple(SPECS)

__all__ = [
    "Plan",
    "RUNTIMES",
    "Result",
    "RuntimeSpec",
    "SPECS",
    "SetupError",
    "Step",
    "apply_plan",
    "detect_repo",
    "known_template_digests",
    "known_workflow_digests",
    "lines",
    "plan_setup",
    "run_setup",
    "script_text",
    "template_digest",
    "to_json",
    "workflow_digest",
    "workflow_text",
]
