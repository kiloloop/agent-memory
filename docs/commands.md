# Command reference

Detailed command contracts for `agent-memory`. Run `agent-memory <command> --help`
for arguments and defaults; nested verbs also have their own help.

The home resolves, first hit wins, from `--home`, `$AGENT_MEMORY_HOME`,
`$OACP_HOME`, the nearest `.agent-memory.json` binding above the working
directory, a workspace marker (a symlink or `workspace.json` whose real path
is `<home>/projects/<name>/workspace.json`), then `~/agent-memory`.

## Status

`status` prints where the home resolves and where its sync stands: the rule
that chose the home, the marker, the allowlist, the tiers, then the tree and
the upstream. It contacts the remote only with `--fetch`, and it exits 1 when
the tree is dirty or diverged (ahead and behind are reported, not failed).

## Doctor

`doctor` runs the two memory setup checks: the debrief store's
layout (presence, canonical paths, staging leftovers, symlinks; it never opens
a record, and a traversal it cannot finish is an error row, never a pass) and
the sync repository (marker, allowlist, tracked and untracked memory files,
the tree with memory changes told apart from changes outside the allowlist,
upstream, remote, last-commit age, per-instance state, overlay ignores).
It reads no memory content, repairs nothing, exits 1 only on an error row,
and points at `memory-lint` when that is installed; `--json` emits the same
rows as data.

## Init

`init` creates the home from the templates bundled in the package: the org
tier (`recent.md`, `decisions.md`, `rules.md`, `events/`, `debriefs/`) and,
with `--project`, that project's tier (`project_facts.md`, `decision_log.md`,
`open_threads.md`, `known_debt.md`, `archive/`). With `--repo` it records a
`.agent-memory.json` binding in that repository last, after every other
check, and refuses to overwrite a binding that points elsewhere. Nothing that
exists is rewritten, so a rerun changes no byte; a template missing from the
installed package is an error, not a silent fallback. No git, no network, no
credentials. `org init` is the org tier alone, for a home that already exists.

## Sync

`enable` makes the home a git repository of its own, puts the sync allowlist at
the head of its `.gitignore` as a managed block (existing lines are kept, lines
an earlier block carried are retired, and the write comes with a before/after
receipt that names them), drops the sync marker, and makes
one commit. With `--remote`, `enable` also pushes that initial commit. `push` commits only what the allowlist selects, as a partial
commit, so anything else staged in the index stays staged and uncommitted;
it refuses a home that is behind or diverged, and a push the remote rejects
leaves the local commit in place and says so. A credential helper a sandbox
blocked is a distinct failure from a rejection -- git prompts for a username
and has no terminal to read it from -- and `push` names it as such; the remedy
is to rerun with the sandbox off. `pull` fast-forwards only when
the tree is clean, not ahead, not diverged, and has an upstream. `clone`
brings a memory repository down; `disable` removes the marker. The network
verbs time out after 30 seconds. The engine never reads memory content,
never merges, and never touches `keys/`. It needs git 2.25 or newer.

## Archive and restore

`archive` moves one supplementary file from a project's `memory/` into
`memory/archive/<UTC timestamp>_<basename>`, and `restore` moves it back into
an active slot that must be empty; the four active files are never archived,
under their own names or under any other name that addresses the same file
(a case variant on a case-insensitive filesystem, a hard link). Both refuse
to replace anything that exists at the instant of the move (the move is a
hard link plus unlink, so bytes and metadata are kept), and both address the
file through directory handles opened one component at a time without
following symlinks, so a symlinked directory or file is refused and a
directory swapped for a symlink after the check cannot redirect the move.
`--dry-run` runs every check, the archive path included, and reports the
same paths. POSIX only; Windows is refused.

## Setup

`setup claude` and `setup codex` install the runtime's session-start memory
hook in a repository: a short script (`.claude/hooks/agent-memory-pull.sh`,
`.codex/hooks/agent-memory-pull.sh`) that runs `agent-memory startup --pull`
and can never block a session (no `agent-memory` on the PATH, or a pull that
fails, is a warning), its registration in `.claude/settings.json` or
`.codex/hooks.json` added once by exact command, and a receipt in the home
(`setup/<runtime>/`, never synced) recording the path, the resolved symlink
target, the digest and the version. The plan is computed in full before a
byte is written (`--dry-run` prints it; `--json` for either), a rerun changes
nothing, and an interrupted run is resumed by running it again. A script
whose digest matches no shipped template is a named conflict and is kept; a
shipped template that lost its execute bit gets it back, and a script that
cannot be made runnable is never registered. Nothing is ever written through
a symlink (a linked script, settings file or hooks directory is reported
with its target), and the exit is 3 when anything was held. The hooks
earlier tooling installed are retired by exact command, and only once the
new hook is registered, so a repository is never left without one; their
scripts are removed only when the settings file was read in full, no
command in it still names the script, the bytes are what that tooling
wrote, and no symlink lies between the repository and the file. Custom
entries are untouched; the codex entry sits beside the kernel's
session-init entry and retires only its pull flag, edited in place in one
simple command (a compound command is left as written and named). No push
hook is ever installed. Beside the hook, `setup` installs the runtime's memory
workflow file, a repository skill (`.claude/skills/agent-memory/SKILL.md`,
`.agents/skills/agent-memory/SKILL.md`) rendered from one shipped text with
the runtime's name and managed by the hook's rules: written once, regenerated
only while its digest is a shipped template, kept and named as a conflict when
edited, never written through a symlink, and recorded in the receipt.

## Startup

`startup --runtime <claude|codex>` prints the session-start manifest: the
project's four active files, then the three curated org files, each with its
readability, size and modification time, and where the sync stands; with
`--pull` it fast-forwards the home first, and the files are described as
the pull left them. `events/`, `debriefs/` and `archive/` are excluded. No
content is included and no file is claimed as read
(`content_injected: false`); the text, notice included, is cut at a
character budget (`--max-chars`, at least 1). The default output is what
the runtime's hook expects on stdout (plain text for claude, the hook JSON
envelope for codex); `--json` is the manifest with its `schema_version`. The
project comes from `--project`, else from the repository's binding or
workspace marker, whichever way the home was chosen: a binding that names a
different home lends no project, and says so in the warnings.

## Capture and recall

`capture` and `recall` are the workflow that file describes. `capture "<decision>"
[--why TEXT] [--source REF] [--agent NAME]` appends one decision to the
project's `decision_log.md`, newest first under today's UTC date heading, with
its provenance (the agent, the UTC time, the source); it touches no other
file, never writes through a symlink (a linked directory or file below the
home is refused, not followed), replaces the file atomically keeping its
mode, and `--dry-run` composes the entry and writes nothing. `recall` prints the files
the startup manifest lists, in its order, with their content: the bounded
read at session start, cut at a character budget (`--max-chars`, default
8000) with a notice, and `archive/`, `events/` and `debriefs/` are never
loaded. Both take the project from `--project`, else from the repository's
binding or workspace marker like `startup`; `--json` for either. Neither
synthesises, searches or indexes anything.

## Debrief write

`debrief write` publishes one session debrief into the home's debrief store,
at `org-memory/debriefs/<project>/<YYYY>/<MM>/<YYYYMMDD>-<agent>-<session>.md`,
under the writer contract of the layout spec: the record is a frontmatter
block (`schema_version`, the identity fields, `started_utc`, `ended_utc`,
`content_sha256` over the exact body bytes, `immutable: true`) plus the body
verbatim; it is staged in a private file, verified through the descriptor that
wrote it, published with an atomic no-replace link, and read back. A
published record is never replaced: a differing record at the same path is a
collision (exit 2, publish under a new session id), an identical one is
idempotent, and the canonical path never holds partial bytes of the record
(one qualification, on platforms without a descriptor-bound link, follows). `--dry-run`
composes and prints the record and touches nothing; `--json` reports the
path, status and hash. Exit 1 on a validation error, 2 on a publication
failure. The publication step is one module, `agent_memory.publication`,
shared by every writer in the package. A staging entry swapped under the
writer for a link to some other file is never the published record: on Linux
the link is bound to the verified descriptor and refuses the orphaned inode,
so nothing foreign is ever visible; elsewhere (macOS, a Linux without
`/proc`) the link is by name, the foreign file is visible under the canonical
name from the link until the identity check takes that name down, and a
writer stopped in that interval leaves it there, which the next writer of the
record reports as a collision. That narrower contract rests on the store
directory not being writable by other users, its default mode, so the swap
needs the owner's own uid; it is a platform limitation. Either way the writer restages and
retries, and a swap that persists is reported with the canonical path absent.

## Event write

`event write` publishes one org-memory event into the home's events store,
at `org-memory/events/<YYYYMMDD>-<HHMMSS>-<slug>.md`: the mechanical writer
the layout spec names, ported from the kernel's `write-event` script and
byte-identical to it for the same inputs. The record is a frontmatter block
of plain scalars (`created_at_utc`, `date`, `agent`, `project`, `type`, then
`source_ref`, `related` and `supersedes` when given), a blank line, the body
with its trailing newlines dropped, and one closing newline. Because the
scalars are plain, a value a YAML reader would not hand back verbatim -- a
control character, a leading indicator, `: ` or ` #` inside, or a string it
re-types such as `true`, `12` or `2026-03-21` -- is refused (exit 1) rather
than written. The body comes from `--body`, from `--body-file <path>` or
`--body-file -` (stdin), or from piped stdin when neither is given. A body
file is read as text, as the script reads it, so its CRLF and CR line endings
become LF and a Windows-authored file lands the same record as its LF twin;
stdin and `--body` are taken as given. `--type` is one of `decision`,
`event`, `rule`; the slug is lowercase alphanumerics and hyphens,
alphanumeric at both ends, no dots. Publication is the writer
contract of `debrief write`, through the same `agent_memory.publication`
module: staged in a private file, verified through the descriptor that wrote
it by a check that parses the frontmatter back, published with an atomic
no-replace link, and read back. An identical record already at the name is
idempotent (exit 0, the file untouched), a differing one is a collision (exit
2, a published record is never replaced), and a failure before the link
leaves `events/` with no partial record and no staging debris. `--dry-run`
composes and prints the record and touches nothing; `--json` reports the
path, status, stamp and identity. The verb appends one event and reads
nothing; synthesis stays with the caller.
