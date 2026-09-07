---
name: agent-memory
description: Cross-session memory for this repository. Use at session start to read the bounded memory context (recall), and whenever a decision is made that a later session must not rediscover (capture). Also use when asked what was decided before.
---

# agent-memory workflow for {runtime}

Managed by `agent-memory setup {runtime}`: a rerun regenerates this file only
while its digest matches a shipped template; an edited file is reported as a
conflict and kept. The rules are the same for every runtime; only the agent
name differs.

## Recall, at session start

The session-start hook printed the startup manifest: the memory files to read,
in order, with their readability only. No content was injected. Before acting
on project work, read the bounded context in one step:

    agent-memory recall

It prints the project's active files (`project_facts.md`, `decision_log.md`,
`open_threads.md`, `known_debt.md`), then the curated org files (`recent.md`,
`decisions.md`, `rules.md`), cut at a character budget (`--max-chars`, default
8000). Cite a decision by its date heading and its text. `archive/`, `events/`
and `debriefs/` are never loaded; read them only when the work needs them.

## Capture, when a decision is made

When the user decides something that a future session must not rediscover,
record it once, in one sentence, with the reason:

    agent-memory capture --agent {runtime} "<the decision>" --why "<the reason>" --source "<PR, issue or message>"

It appends a dated entry, newest first, to the project's `decision_log.md`,
with provenance: the agent, the UTC time and the source. Capture only what the
user decided, not what you propose; confirm first when in doubt. One decision
per entry. Stable facts go to `project_facts.md` by hand, not through capture.

## Out of scope

No synthesis, search or index; no writes to the org tier; no push. Syncing the
home is `agent-memory push`, run deliberately, never from this workflow.
