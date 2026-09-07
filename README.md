# agent-memory

Cross-session memory for coding agents — plain files, git-native, no server.

## Why

Agents start sessions blank. Keep facts, decisions, and unfinished work in
Markdown you can read and edit.

Memory travels **between agents** via project files, **between projects** via
org rules, and **between machines** via optional git sync. Claude Code and Codex
share the store. No database, daemon, server, or runtime Python dependencies.

## Quick Start

First time: install and bind. Every session after: recall context, capture decisions.

### With your coding agent

Paste this into Claude Code or Codex from your repository:

```text
Set up cross-session memory with agent-memory-cli from
https://pypi.org/project/agent-memory-cli/ and its source,
https://github.com/kiloloop/agent-memory.

Install with Python 3.10+. Ask for my home and project name, initialize and
bind this repository, and git-ignore the binding. Set up my runtime's hook
and workflow; report conflicts.

Capture a decision I provide, with its reason and runtime. Show status and
doctor. Next session here, recall context and report any truncation or sync
warning. Ask for my remote before enabling optional sync; keep pushing explicit.
```

### Manually

Python 3.10+ is required; sync needs git 2.25+, hooks use Bash, and
`archive`/`restore` are POSIX-only.

1. **Install** with `uv tool install agent-memory-cli` or
   `python -m pip install agent-memory-cli`. The distribution and the command
   differ: installing `agent-memory-cli` gives you `agent-memory`. Put it on
   your agent's PATH.
2. **Bind**, from your repository root:

   ```bash
   agent-memory init --home "$HOME/agent-memory" --project my-app --repo .
   ```

   Git-ignore `.agent-memory.json`. Stay here; unset `AGENT_MEMORY_HOME`/`OACP_HOME` or point them at this
   home: they precede the binding.
3. **Set up** the runtime you use, then enable the hook there if required:

   ```bash
   agent-memory setup claude
   # Or:
   agent-memory setup codex
   ```

4. **Record now; recall next session** in this repository:

   ```bash
   agent-memory capture "Use SQLite for the cache." --why "no daemon" --agent codex
   agent-memory recall
   agent-memory status
   agent-memory doctor
   ```

   Use `--agent claude` if appropriate; recall also works now for inspection.
5. **Optionally sync.** Use an empty private remote. `enable --remote` pushes
   the initial commit; substitute its URL:

   ```bash
   agent-memory enable --remote git@github.com:YOUR_ORG/agent-memory-store.git
   agent-memory push --agent codex
   # After another machine pushes, with a clean local tree:
   agent-memory pull
   ```

   Elsewhere, `agent-memory clone <remote-url> --home <local-home>`, then repeat
   binding and setup. Bind each machine separately; push explicitly.

## How It Works

![Four project memory files with sample Markdown](https://raw.githubusercontent.com/kiloloop/agent-memory/main/docs/images/project-memory.jpg)

*Illustrative files; not a bundled application UI.*

OACP defines the layout; this tool implements it. The four active files in
`projects/<name>/memory/` hold notes such as these trimmed samples:

| File | Purpose and sample |
| --- | --- |
| `project_facts.md` | Stable facts: FastAPI backend; Postgres; no PII in logs. |
| `decision_log.md` | Choices: 2026-05-09 — retry 5xx three times, with backoff and jitter. |
| `open_threads.md` | Work and owners: OAuth refresh race — waiting on Codex. |
| `known_debt.md` | Problems: replace the hard-coded session TTL with a setting. |

Date entries; supersede decisions by adding new ones. Close or pause threads,
write for humans, and distill transcripts. Edit other files directly; promote
debt to a thread when work starts.

`org-memory/` sits beside `projects/`: `recent.md`, `decisions.md`, and
`rules.md` carry shared context; `events/` and `debriefs/` hold records.
Most notes belong to a project. Use org memory only across repositories.

Home resolution: `--home` → `AGENT_MEMORY_HOME` → `OACP_HOME` → nearest ancestor
`.agent-memory.json` → workspace marker → `~/agent-memory`. A workspace marker
points into a home's `projects/` tree. Project selection uses `--project` or a
matching binding/marker. OACP is not required.

Sync makes the home a git repository with an allowlist and `.oacp-memory-repo`
marker. `push` commits selected paths; `pull` fast-forwards a clean tree that
is not ahead or diverged. No merges; keys and setup receipts stay local.
Network verbs time out after 30 seconds.

`setup` installs a SessionStart hook and memory workflow. `startup` lists
metadata for the four project files, then the three curated org files; it
injects no content. `--pull` refreshes first, warning on failure. The workflow
tells the agent to run `recall` for an 8,000-character bounded read. Raise
`--max-chars` or read remaining files directly when cut. `capture` records
decisions during work. At the end, update threads and debt, optionally publish
a summary with `debrief write`, and explicitly `push`; no push hook is installed.

Startup and recall exclude `archive/`, `events/`, and `debriefs/`. This is not
a vector database, RAG pipeline, or chat-history store: no embeddings,
similarity queries, synthesis, or indexing. Recall reads a fixed file set.

### Commands

| Command | Purpose |
| --- | --- |
| [`status`][status] | Inspect home and sync. |
| [`doctor`][doctor] | Check health; repair nothing. |
| [`init`][init] | Scaffold and bind. |
| [`org init`][init] | Scaffold org memory. |
| [`enable`][sync] | Enable git sync. |
| [`clone`][sync] | Clone a memory remote. |
| [`pull`][sync] | Fast-forward from upstream. |
| [`push`][sync] | Commit selected files and push. |
| [`disable`][sync] | Disable sync. |
| [`archive`][archive-and-restore] | Archive a supplementary file. |
| [`restore`][archive-and-restore] | Restore to an empty slot. |
| [`setup`][setup] | Install runtime integration. |
| [`startup`][startup] | Print the metadata manifest. |
| [`capture`][capture-and-recall] | Record a decision. |
| [`recall`][capture-and-recall] | Read bounded context. |
| [`debrief write`][debrief-write] | Publish a session summary. |

[status]: https://github.com/kiloloop/agent-memory/blob/main/docs/commands.md#status
[doctor]: https://github.com/kiloloop/agent-memory/blob/main/docs/commands.md#doctor
[init]: https://github.com/kiloloop/agent-memory/blob/main/docs/commands.md#init
[sync]: https://github.com/kiloloop/agent-memory/blob/main/docs/commands.md#sync
[archive-and-restore]: https://github.com/kiloloop/agent-memory/blob/main/docs/commands.md#archive-and-restore
[setup]: https://github.com/kiloloop/agent-memory/blob/main/docs/commands.md#setup
[startup]: https://github.com/kiloloop/agent-memory/blob/main/docs/commands.md#startup
[capture-and-recall]: https://github.com/kiloloop/agent-memory/blob/main/docs/commands.md#capture-and-recall
[debrief-write]: https://github.com/kiloloop/agent-memory/blob/main/docs/commands.md#debrief-write

## Examples

Scratch run in `/private/tmp/am-readme-demo`: init/startup/recall excerpts;
other outputs complete.

```console
$ agent-memory init --home memory --project demo
Initialized memory home: /private/tmp/am-readme-demo/memory
```

```console
$ agent-memory startup --home memory --project demo --runtime claude --max-chars 420
agent-memory startup (claude): home /private/tmp/am-readme-demo/memory (flag), project demo (flag)
Project memory, read in this order (states are readability only; no content is injected):
```

```console
$ agent-memory capture 'Use SQLite for the cache.' --why 'no daemon' --agent codex --home memory --project demo
captured: /private/tmp/am-readme-demo/memory/projects/demo/memory/decision_log.md (## 2026-09-07)
- **Use SQLite for the cache.** Why: no daemon (codex, 2026-09-07T01:53:42Z)
```

```console
$ agent-memory recall --home memory --project demo --max-chars 850
## 2026-09-07

- **Use SQLite for the cache.** Why: no daemon (codex, 2026-09-07T01:53:42Z)
```

```console
$ agent-memory status --home memory
home: memory
source: flag
exists: yes
marker: absent
gitignore: canonical
org-memory: present
projects: 1 with a memory dir
sync: not configured
```

```console
$ agent-memory doctor --home memory
[+] Org Memory
    [+] org-memory/debriefs/ — present
    [+] debriefs/ — empty store, nothing to validate

[-] Memory Sync
    [-] .oacp-memory-repo — not configured; memory sync hooks are disabled
        Run: agent-memory enable [--remote URL]

No issues found.
```

## Project

- [PyPI package: agent-memory-cli](https://pypi.org/project/agent-memory-cli/)
- [Source](https://github.com/kiloloop/agent-memory)
- [OACP](https://github.com/kiloloop/oacp)

## License

Apache-2.0. See [LICENSE](https://github.com/kiloloop/agent-memory/blob/main/LICENSE).

## Development

Activate `.venv` before running the two `make` commands.

    python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
    make preflight     # lint, test, build
    make wheel-check   # install the built wheel in a throwaway venv and prove it operates a home
