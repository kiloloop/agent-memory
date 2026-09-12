# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Cross-session memory for coding agents: plain files, git-native, no server."""

from .home import HomeError, HomeResolution, resolve_home

__version__ = "0.1.1"

__all__ = ["HomeError", "HomeResolution", "__version__", "resolve_home"]
