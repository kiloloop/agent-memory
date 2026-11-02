# agent-memory skill

Teach a coding agent the `agent-memory` workflow: recall the project's
memory at session start, record the user's decisions as they are made, and
keep the memory home tidy, recorded and synced.

## What it is

The CLI does the work. The skill tells an agent which verb to run when, and
which uses to avoid:

- recall before acting, and capture only what the user decided;
- archive supplementary files, never the four active ones;
- treat debriefs and events as append-only records;
- push deliberately at the end of a session, knowing that a pull on a home
  with local changes, the session-start hook's included, commits and
  delivers them too.

It lives in the same repository as the CLI and ships in the same release, so
each copy describes the release it came from.

The claim it makes, and the repository's tests check: a decision captured in
one session is in the next session's `recall`, while the session-start
manifest lists that file without its content. Archived files and org events
stay out of that read, and an active memory file cannot be archived.

## What it looks like

The output below is real. It was captured against a scratch home, with
`--home` and `--project` given explicitly:

```console
$ agent-memory capture "Cache with SQLite." --why "no daemon to run" --source "PR 12" --agent claude --home /private/tmp/claude/agent-memory-skill/home --project demo
captured: /private/tmp/claude/agent-memory-skill/home/projects/demo/memory/decision_log.md (## 2026-11-01)
- **Cache with SQLite.** Why: no daemon to run (claude, 2026-11-01T03:28:50Z, source: PR 12)

$ agent-memory startup --runtime claude --home /private/tmp/claude/agent-memory-skill/home --project demo
agent-memory startup (claude): home /private/tmp/claude/agent-memory-skill/home (flag), project demo (flag)
Project memory, read in this order (states are readability only; no content is injected):
  1. projects/demo/memory/project_facts.md: readable, 196 bytes, modified 2026-11-01T03:28:50Z
  2. projects/demo/memory/decision_log.md: readable, 205 bytes, modified 2026-11-01T03:28:50Z
  3. projects/demo/memory/open_threads.md: readable, 28 bytes, modified 2026-11-01T03:28:50Z
  4. projects/demo/memory/known_debt.md: readable, 240 bytes, modified 2026-11-01T03:28:50Z
Org memory, curated context; consult what governs the work before doing it (not read by default):
  5. org-memory/recent.md: readable, 389 bytes, modified 2026-11-01T03:28:50Z
  6. org-memory/decisions.md: readable, 287 bytes, modified 2026-11-01T03:28:50Z
  7. org-memory/rules.md: readable, 283 bytes, modified 2026-11-01T03:28:50Z
Excluded by default: org-memory/events/, org-memory/debriefs/, projects/demo/memory/archive/

$ agent-memory recall --home /private/tmp/claude/agent-memory-skill/home --project demo --max-chars 1100
agent-memory recall: home /private/tmp/claude/agent-memory-skill/home, project demo; content follows in manifest order, cut at 1100 characters.

--- projects/demo/memory/project_facts.md (196 bytes, modified 2026-11-01T03:28:50Z) ---
# Project Facts

<!-- Stable facts about this project: what it is, where it runs, who owns what. -->
<!-- Rewrite in place as facts change; the history of a change belongs in decision_log.md. -->

--- projects/demo/memory/decision_log.md (205 bytes, modified 2026-11-01T03:28:50Z) ---
# Decision Log

<!-- One dated entry per decision, newest first: what was decided, and why. -->

## 2026-11-01

- **Cache with SQLite.** Why: no daemon to run (claude, 2026-11-01T03:28:50Z, source: PR 12)

--- projects/demo/memory/open_threads.md (28 bytes, modified 2026-11-01T03:28:50Z) ---
# Open Threads

- None yet.

--- projects/demo/memory/known_debt.md (240 bytes, modified 2026-11-01T03:28:50Z) ---
# Known Debt

Use this file to track verified, unresolved project debt
[agent-memory: recall cut at 1100 characters; raise --max-chars or read the remaining files directly]

$ agent-memory archive demo decision_log.md --home /private/tmp/claude/agent-memory-skill/home
agent-memory: error: cannot archive standard active memory file: decision_log.md
```

The last command exits `1`. The cut notice is the signal the skill tells an
agent to act on: raise `--max-chars` or read the rest directly.

## Run it

Install the release this skill ships in, then copy this directory into your
agent's skills directory:

```bash
python -m pip install agent-memory-cli==0.1.5
mkdir -p ~/.claude/skills && cp -R skills/agent-memory ~/.claude/skills/   # Claude Code
mkdir -p ~/.agents/skills && cp -R skills/agent-memory ~/.agents/skills/   # Codex
```

`agent-memory --version` should print `agent-memory 0.1.5`. In a repository,
`agent-memory setup claude` or `setup codex` installs the session-start hook
that pulls the home and prints the manifest. The skill teaches the rest. The
full procedure is in [`SKILL.md`](SKILL.md).

## Choices worth knowing about

- **Released with the CLI.** The skill names the release it describes in its
  install line, and the repository's tests fail when that disagrees with the
  package version. `agent-memory --version` is the check: an older CLI needs
  that release installed, a newer one needs the skill from its release.
- **One skill, one name.** `agent-memory setup` in release 0.1.4 also writes a
  shorter workflow skill named `agent-memory` into the repository
  (`.claude/skills/agent-memory/SKILL.md`, `.agents/skills/agent-memory/SKILL.md`).
  Later releases stop writing it but never delete an existing copy. A copy left
  from an earlier setup has the same name as this skill and can hide it, so
  delete that copy when you install this one.
- **The hook syncs; the skill teaches.** The session-start hook keeps doing the
  pull, and on a home with local changes that pull commits and delivers them.
  The skill adds no hook of its own; `push` stays a deliberate end-of-session
  step.
- **Runtime-neutral.** Commands take `--agent <claude|codex>` and, for
  `recall`, `--runtime codex` from Codex. The skill does not bake in a runtime
  name.

## What it cannot see

- Whether a remembered decision is still right, or whether a fact is true.
  `recall` prints files; it does not judge them.
- Anything outside the fixed read set: supplementary files, `archive/`,
  `events/` and `debriefs/` are read only when an agent opens them by name.
- Search, synthesis, embeddings or indexes. The CLI has none.
- Memory files whose structure has drifted from the templates. The skill says
  where things go, not how to repair a file.

## Verify the skill

`tests/test_skill.py` runs with the rest of the suite (`make test`). It fails
when `SKILL.md` or this README names a version other than the one the package
releases, or shows a command or option the CLI does not have.

The behaviour the skill relies on is tested with the commands themselves:
capture then recall across two sessions and the recall exclusions in
`tests/test_workflow.py`, the content-free startup manifest and a session-start
pull that commits and delivers a local change in `tests/test_startup.py`,
archive refusal and round trip in `tests/test_archive.py`, and event records in
`tests/test_events.py`.
