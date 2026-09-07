#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Print the changelog section for one release, as GitHub Release notes.

The release workflow feeds this to ``gh release create --notes-file``. GitHub's own
``--generate-notes`` cannot serve here: the public repository receives each version as a
single squashed commit, so the generated summary is one line no matter how much the
release contains. The changelog section already says what shipped, one line per change.

Two transformations make a changelog section stand alone on a release page:

1. Relative links (``docs/commands.md#init``) are rewritten against the tag. A release
   body is not rendered relative to the repository root, so a relative target 404s; and
   pinning to the tag rather than to ``main`` keeps old release notes pointing at the
   documentation as it stood for that version.
2. The section heading itself is dropped — the release already carries the version.

Exits 1 when the section is absent, so the caller can fall back rather than publish a
release with an empty body.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

LINK = re.compile(r"\]\(([^)]+)\)")


def extract(changelog: str, version: str) -> str | None:
    """Return the body of ``## <version>``, or None when no such section exists."""
    lines = changelog.split("\n")
    start = None
    for i, line in enumerate(lines):
        # "## 0.1.0 - 2026-09-06" and "## [0.1.0] - ..." both name this version.
        if line.startswith("## ") and re.match(rf"##\s+\[?{re.escape(version)}\]?\b", line):
            start = i + 1
            break
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body).strip() or None


def absolutise(notes: str, repository: str, tag: str) -> str:
    """Rewrite repo-relative link targets to absolute URLs pinned at ``tag``."""
    base = f"https://github.com/{repository}/blob/{tag}/"
    return LINK.sub(
        lambda m: f"]({m.group(1)})" if m.group(1).startswith("http") else f"]({base}{m.group(1)})",
        notes,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="release tag, e.g. v0.1.0")
    parser.add_argument("--repository", required=True, help="owner/repo, for absolute links")
    parser.add_argument("--changelog", default="CHANGELOG.md", type=Path)
    args = parser.parse_args()

    version = args.tag[1:] if args.tag.startswith("v") else args.tag
    notes = extract(args.changelog.read_text(encoding="utf-8"), version)
    if notes is None:
        print(f"no changelog section for {version} in {args.changelog}", file=sys.stderr)
        return 1
    print(absolutise(notes, args.repository, args.tag))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
