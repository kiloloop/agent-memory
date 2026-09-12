# Changelog

All notable changes to agent-memory are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.1.1 - 2026-09-12

### Added

- `event write` publishes one org-memory event under `org-memory/events/`, never replacing a published record ([event](docs/commands.md#event-write)).
- A `v*` tag attaches the built sdist and wheel to the GitHub Release ([releases](https://github.com/kiloloop/agent-memory/releases)).

### Fixed

- A nested `.gitignore` or `.oacp-memory-repo` no longer blocks `pull`; the allowlist anchors both names to the home root ([sync](docs/commands.md#sync)).
- `enable` retires superseded lines when it updates a home's `.gitignore`, and the receipt names them ([sync](docs/commands.md#sync)).
- `doctor` scopes its working-tree row to the allowlist: memory changes read DIRTY, and changes elsewhere are named as outside it ([doctor](docs/commands.md#doctor)).
- `push` and `doctor` keep both paths of a rename git detects in the working tree; the source path was previously misread ([sync](docs/commands.md#sync)).
- `push` names a credential helper a sandbox blocked as its own failure, with the remedy, instead of reporting it as a rejected push ([sync](docs/commands.md#sync)).

## 0.1.0 - 2026-09-06

### Added

- The `agent-memory-cli` distribution installs the `agent-memory` command, with no runtime dependencies ([Quick Start](README.md#quick-start)).
- `agent-memory init` builds the two-tier memory home from bundled templates and binds a repository ([init](docs/commands.md#init)).
- Home resolution follows a fixed precedence; an ancestor it cannot inspect is an error, not a fall-through ([status](docs/commands.md#status)).
- `agent-memory setup claude|codex` installs the runtime's session-start hook and memory workflow file ([setup](docs/commands.md#setup)).
- `agent-memory startup` prints the session-start read manifest: the memory files in order, never their content ([startup](docs/commands.md#startup)).
- `capture` records one decision; `recall` prints the manifest's files, cut at a character budget ([recall](docs/commands.md#capture-and-recall)).
- `debrief write` publishes a session debrief atomically, never replacing a published record ([debrief](docs/commands.md#debrief-write)).
- `archive` and `restore` move a memory file in and out of `memory/archive/`, following no symlink ([archive](docs/commands.md#archive-and-restore)).
- `enable`, `clone`, `pull`, `push` and `disable` operate a memory home as its own git repository ([sync](docs/commands.md#sync)).
- A memory commit carries only the paths the sync allowlist selects; `keys/` is refused at any depth ([sync](docs/commands.md#sync)).
- `status` reports which home resolved, whether its layout is in place and where sync stands ([status](docs/commands.md#status)).
- `doctor` checks the debrief store and the sync repository; it reads no memory content and repairs nothing ([doctor](docs/commands.md#doctor)).
- A `v*` tag publishes the distribution to PyPI with build attestations ([PyPI](https://pypi.org/project/agent-memory-cli/)).
