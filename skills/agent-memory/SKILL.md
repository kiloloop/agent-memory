---
name: agent-memory
description: Cross-session memory for coding agents through the agent-memory CLI. Use at session start to recall the project's bounded memory, when the user makes a decision a later session must not rediscover, when asked what was decided before, and when tidying, recording or syncing memory (archive, debrief and event writes, pull, push).
---

# agent-memory

Memory is plain Markdown in a home directory that persists across sessions,
agents and machines. The `agent-memory` command reads and writes it. This
skill says which verb to run when, and what each one must not be used for.

## Install and check

This skill ships in the agent-memory release it describes. Install that
release:

```bash
python -m pip install agent-memory-cli==0.1.5
```

`uv tool install agent-memory-cli==0.1.5` works too. The distribution is
`agent-memory-cli`; the command it installs is `agent-memory`.

Before relying on anything below, confirm the version:
`agent-memory --version` must print `agent-memory 0.1.5`. If it prints an
older version, install this one. If it prints a newer one, replace this skill
with the `skills/agent-memory/` directory from that release's tag.

## Where memory lives

- **Home.** It resolves from `--home`, then `$AGENT_MEMORY_HOME`, then
  `$OACP_HOME`, then the nearest `.agent-memory.json` binding above the working
  directory, then a workspace marker, then `~/agent-memory`. When a result
  looks wrong, run `agent-memory status` first: it names the home and the rule
  that chose it.
- **Project tier** (`projects/<name>/memory/`). Four active files:
  `project_facts.md` (stable facts), `decision_log.md` (decisions, newest
  first), `open_threads.md` (work in flight and its owner) and `known_debt.md`
  (known problems). Retired files go in `archive/`.
- **Org tier** (`org-memory/`). `recent.md`, `decisions.md` and `rules.md` are
  curated context shared across projects. `events/` and `debriefs/` hold
  immutable records.

Most notes belong to a project. Use the org tier only for what another
repository needs.

## Session start: recall

A repository set up with `agent-memory setup claude` or `setup codex` runs a
session-start hook. The hook pulls the home (see Sync for what that pull
publishes) and prints a manifest listing which
files to read, with their readability only. It injects no file content. Before
acting on project work, read the bounded context:

```bash
agent-memory recall
```

It prints the four project files, then the three curated org files, cut at
8000 characters. From Codex, add `--runtime codex`. When the output reports a
cut, raise `--max-chars` or read the remaining files directly. `archive/`,
`events/` and `debriefs/` are never loaded. Open one of their files by name
only when the work needs it, never by reading the directory recursively.

Cite a decision by its date heading and its text. Say that you read a file
only when you actually read it.

## During work: capture decisions

When the user decides something a later session must not rediscover, record
it once:

```bash
agent-memory capture "<the decision, one sentence>" --why "<the reason>" --source "<PR, issue or message>" --agent <claude|codex>
```

- Capture what the user decided, not what you propose. If you are not sure it
  was decided, ask.
- Write one decision per entry. To change a decision, capture the new one. Never
  edit or delete the old entry.
- `--dry-run` composes the entry and writes nothing.
- `capture` writes only `decision_log.md`. Edit the other active files by hand:
  stable facts go in `project_facts.md`, work in flight in `open_threads.md`,
  problems in `known_debt.md`. Close or pause a thread when its state changes.

## Tidy: archive and restore

`agent-memory archive <project> <file>` moves a supplementary file from
`memory/` into `memory/archive/<UTC timestamp>_<file>`.
`agent-memory restore <project> <archived file>` moves it back, but only into a
slot that is empty. The four active files are never archived. Run either with
`--dry-run` first. An archived file drops out of the session-start read.

## Records: debriefs and events

Both verbs append and never replace. A different record at the same path is a
collision (exit `2`), and an identical one is accepted unchanged. Use
`--dry-run` to see the record before writing it.

- `agent-memory debrief write --project P --agent A --runtime R --session S
  --started-utc T0 --ended-utc T1 --body-file F` publishes a session summary
  under `org-memory/debriefs/`. The session id is 1–32 lowercase alphanumerics.
- `agent-memory event write --agent A --project P --type decision|event|rule
  --slug S --body-file F` publishes one org-memory event under
  `org-memory/events/`.

Neither verb curates. Folding events into `recent.md`, `decisions.md` or
`rules.md` is a deliberate edit, made by whoever owns that context.

## Sync: pull and push

This section applies only to a home with sync enabled, through
`agent-memory enable --remote <url>`; `status` shows whether it is.

- **Pull** runs at session start through the hook (`startup --pull`). On a
  clean home it only updates. On a home with local memory changes it also
  commits the allowlisted changes under the runtime's name and delivers them
  to the remote. By hand, `agent-memory pull` updates a clean home, and
  `pull --agent <name>` does the same commit-and-deliver on a dirty one.
- **Push** is the deliberate end-of-session step, once threads and debt are
  up to date: `agent-memory push --agent <name>`. `setup` installs no push
  hook, but the next session's start publishes whatever this session left
  uncommitted. Treat every memory edit as headed for the remote.
- Both commit only allowlisted memory paths, and anything else in the home stays
  as it is. A rebase conflict aborts and keeps the local commit: report the
  paths it names and do not resolve them by guessing. A credential helper
  blocked by a sandbox is reported as such; rerun outside the sandbox.

## Set up and check health

- `agent-memory init --home <path> --project <name> --repo .` creates the home
  and binds the repository. Git-ignore `.agent-memory.json`.
- `agent-memory setup claude` or `setup codex` installs the session-start hook.
  It does not install this skill.
- `agent-memory status` exits `1` when the tree is dirty or diverged, or a
  prerequisite failed. `agent-memory doctor` exits `1` on an error row. Neither
  repairs anything. Read the row and fix the cause rather than retrying.

## Report

After running a verb, report the command, the home and project it resolved,
the exit status, and any warning: a recall that was cut, a sync state other
than synced, or a conflict.

The CLI does no search, synthesis, embedding or indexing. Recall reads a fixed
set of files.
