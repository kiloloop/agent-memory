# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Sync a memory home through plain git.

The engine owns the sync marker, the managed block of the home's
``.gitignore``, the git state readout and the verbs ``init``, ``clone``,
``pull``, ``push`` and ``disable``. It never reads memory content, never
merges and never touches ``keys/``.

The publication boundary
------------------------
A memory commit holds the selected paths and nothing else. The selection is
``git status`` limited to the layout's sync allowlist, and every candidate
must pass the layout's path predicate, which denies a never-synced name such
as ``keys/`` at any depth whatever the ignore file says; if anything selected
fails it the publish is refused. Otherwise exactly that selection is staged
and committed as a partial commit, every name taken literally (a project
called ``a*`` is a name, not a pattern), so whatever else the index holds --
a runtime file somebody staged by hand -- stays staged, uncommitted and
reported. The home must be the root of its own git worktree; a home nested
inside another repository is refused before anything is written.

``.gitignore`` is never replaced wholesale. The canonical allowlist is a
managed block that ``init`` puts at the head of the file when it is missing
or out of date and leaves alone when it is current, keeping every other line
except the ones an earlier block carried and the current one retired, and
every write comes back with a before/after receipt that names them.

The network verbs (fetch, pull, push, clone) run under a 30 s timeout.
``pull`` fast-forwards only when the tree is clean, not ahead, not diverged
and an upstream exists. ``push`` refuses a repository that is behind or
diverged, and a push the remote rejects leaves the local commit in place and
says so: a local-only commit and remote delivery are distinct outcomes.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

from . import layout
from .git_runner import GitResult, GitRunner, run_git

MARKER_FILE = layout.MARKER_FILE
GITIGNORE_FILE = layout.GITIGNORE_FILE
MARKER_TEXT = "agent-memory sync repository. Remove this file to disable syncing locally.\n"
NETWORK_TIMEOUT_SECONDS = 30
DEFAULT_REMOTE = "origin"

#: The agent name a commit is published under, when the caller does not pass one.
ENV_AGENT = "AGENT_MEMORY_AGENT"
_AGENT_FALLBACK_ENV = ("AGENT_NAME", "USER")
UNKNOWN_AGENT = "unknown"

#: git's own words when no credential helper produced credentials and it fell back
#: to prompting. On its own this is also what an unconfigured remote looks like.
_CREDENTIAL_PROMPT_FAILURES = ("could not read username for", "could not read password for")
#: What the helper itself left behind when a sandbox blocked its `op` call. Every
#: entry must be helper-origin: paired with a prompt failure above, this is the
#: signature -- and not a rejected push. Text git emits on its own never belongs
#: here, however sandbox-flavoured it reads. "Device not configured" is the trap:
#: it is macOS's errno for git's own terminal fallback, so it accompanies *every*
#: prompt failure on the platform and identifies nothing.
_SANDBOXED_HELPER_TELLS = ("1password document", "osstatus")
_HELPER_BLOCKED_LINE = (
    "memory push: the git credential helper could not run under the sandbox; its `op` call was "
    "blocked, so git was left without credentials for the remote. Rerun with the sandbox off. "
    "The commit remains local."
)


class SyncError(Exception):
    """A precondition failed; nothing was changed."""


@dataclass(frozen=True)
class GitState:
    """Where the repository stands against its upstream, after a fetch."""

    has_remote: bool
    has_upstream: bool
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    dirty: bool = False
    fetch_failed: bool = False
    fetch_timed_out: bool = False
    fetch_output: str = ""

    @property
    def diverged(self) -> bool:
        return self.ahead > 0 and self.behind > 0


@dataclass(frozen=True)
class GitignoreReceipt:
    """What :func:`ensure_gitignore` found and what it left behind."""

    path: Path
    #: ``created``, ``unchanged`` or ``updated``.
    action: str
    before: Optional[str]
    after: str
    #: Lines of an earlier managed block that the update dropped, in file order.
    retired: Tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.before != self.after

    def lines(self) -> List[str]:
        if not self.changed:
            return [f"{GITIGNORE_FILE}: managed block present; left unchanged."]
        if self.before is None:
            return [f"{GITIGNORE_FILE}: created with the managed block."]
        kept = self.after[len(layout.gitignore_text()) :]
        out = [
            f"{GITIGNORE_FILE}: managed block added at the top; "
            f"{len(kept.splitlines())} existing line(s) kept after it.",
        ]
        if self.retired:
            out.append(
                f"{GITIGNORE_FILE}: {len(self.retired)} superseded line(s) retired: {', '.join(self.retired)}"
            )
        out += [
            f"{GITIGNORE_FILE} before:",
            *_indent(self.before.splitlines() or ["(empty)"]),
            f"{GITIGNORE_FILE} after:",
            *_indent(self.after.splitlines()),
        ]
        return out


@dataclass(frozen=True)
class Outcome:
    """The result of a verb: a distinct status, whether it counts as success, and what to say."""

    status: str
    ok: bool
    lines: Tuple[str, ...] = ()
    #: Paths the verb committed, home-relative.
    committed: Tuple[str, ...] = ()
    #: Paths that were staged outside the selection and were left exactly as found.
    preserved: Tuple[str, ...] = ()
    receipt: Optional[GitignoreReceipt] = None


# --- marker and ignore file -------------------------------------------------


def marker_path(home: Path) -> Path:
    return home / MARKER_FILE


def is_configured(home: Path) -> bool:
    return marker_path(home).is_file()


def write_marker(home: Path) -> bool:
    """Create the marker if it is missing; an existing marker keeps its bytes."""
    path = marker_path(home)
    if path.is_file():
        return False
    path.write_text(MARKER_TEXT, encoding="utf-8")
    return True


def ensure_gitignore(home: Path) -> GitignoreReceipt:
    """Put the managed block at the head of ``.gitignore`` unless it is already current.

    The block is the canonical allowlist from :func:`layout.gitignore_text`.
    A missing file is created with the block alone, so a fresh home's file is
    byte-identical to the canonical text. A file that already contains the
    block, contiguous and on line boundaries, and none of the lines an earlier
    block carried (:data:`layout.SUPERSEDED_GITIGNORE_LINES`) is left untouched
    wherever the block sits. Otherwise the block goes first and every line of
    the existing file that is neither a block line nor a superseded one follows
    it verbatim: custom rules survive, a file carrying an older version of the
    block is brought up to date without duplicating its lines, and the lines
    that version retired go with it -- git reads an ignore file last-match-wins,
    so a retired rule left after the block would keep its old effect. The
    receipt names what was retired.
    """
    path = home / GITIGNORE_FILE
    block = layout.gitignore_text()
    superseded = set(layout.SUPERSEDED_GITIGNORE_LINES)
    try:
        before: Optional[str] = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        before = None
    retired: Tuple[str, ...] = ()
    if before is None:
        after, action = block, "created"
    else:
        lines = before.replace("\r\n", "\n").splitlines()
        retired = tuple(dict.fromkeys(line for line in lines if line in superseded))
        if _contains_block(before, block) and not retired:
            after, action = before, "unchanged"
        else:
            managed = set(block.splitlines())
            kept = [line for line in lines if line not in managed and line not in superseded]
            after = block + ("\n".join(kept) + "\n" if kept else "")
            action = "updated"
    if after != before:
        path.write_text(after, encoding="utf-8")
    return GitignoreReceipt(path, action, before, after, retired)


def _contains_block(text: str, block: str) -> bool:
    normalized = text.replace("\r\n", "\n")
    return normalized.startswith(block) or f"\n{block}" in normalized


def gitignore_has_managed_block(text: str) -> bool:
    """Whether ``text`` carries the managed allowlist block, contiguous and on line boundaries."""
    return _contains_block(text, layout.gitignore_text())


# --- git readout ------------------------------------------------------------


def is_git_repo(home: Path, runner: Optional[GitRunner] = None) -> bool:
    return _git(home, ["rev-parse", "--is-inside-work-tree"], runner).ok


def worktree_root(home: Path, runner: Optional[GitRunner] = None) -> Optional[Path]:
    """The root of the worktree ``home`` sits in, or ``None`` outside any repository."""
    result = _git(home, ["rev-parse", "--show-toplevel"], runner)
    if not result.ok or not result.stdout.strip():
        return None
    return Path(result.stdout.strip())


def git_state(home: Path, *, runner: Optional[GitRunner] = None, fetch: bool = True) -> GitState:
    dirty = bool(_status_porcelain(home, runner))
    remote = _has_remote(home, runner)
    fetch_failed = fetch_timed_out = False
    fetch_output = ""
    if fetch and remote:
        fetched = _git(home, ["fetch", "--quiet"], runner, timeout=NETWORK_TIMEOUT_SECONDS)
        fetch_failed = not fetched.ok
        fetch_timed_out = fetched.timed_out
        fetch_output = fetched.output
    upstream = _upstream(home, runner)
    ahead = behind = 0
    if upstream:
        counted = _git(home, ["rev-list", "--left-right", "--count", f"HEAD...{upstream}"], runner)
        parts = counted.stdout.split()
        if counted.ok and len(parts) >= 2:
            ahead, behind = int(parts[0]), int(parts[1])
        else:
            fetch_failed, fetch_output = True, counted.output
    return GitState(
        has_remote=remote,
        has_upstream=bool(upstream),
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        dirty=dirty,
        fetch_failed=fetch_failed,
        fetch_timed_out=fetch_timed_out,
        fetch_output=fetch_output,
    )


# --- the verbs --------------------------------------------------------------


def init(
    home: Path,
    *,
    remote: Optional[str] = None,
    agent: Optional[str] = None,
    runner: Optional[GitRunner] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Outcome:
    """Make ``home`` a memory repository: git, the managed ignore block, the marker, one commit.

    Rerunning on an initialized home changes nothing it does not have to:
    the ignore block is only added when missing, the marker keeps its bytes,
    and the commit covers only what the allowlist selects.
    """
    home = _home(home)
    home.mkdir(parents=True, exist_ok=True)
    if is_git_repo(home, runner):
        _require_root(home, runner)
    else:
        result = _git(home, ["init", "--quiet"], runner)
        if not result.ok:
            raise SyncError(f"git init failed: {result.output}")
    receipt = ensure_gitignore(home)
    lines = receipt.lines()
    if write_marker(home):
        lines.append(f"{MARKER_FILE}: created.")
    if remote:
        verb = "set-url" if _git(home, ["remote", "get-url", DEFAULT_REMOTE], runner).ok else "add"
        result = _git(home, ["remote", verb, DEFAULT_REMOTE, remote], runner)
        if not result.ok:
            raise SyncError(f"git remote {verb} failed: {result.output}")
        lines.append(f"remote {DEFAULT_REMOTE}: {remote}")

    published = _publish(home, agent=agent, runner=runner, env=env)
    lines.extend(published.lines)
    if not published.ok:
        return _with(published, lines=lines, receipt=receipt)
    if not remote:
        return Outcome("local_only", True, tuple(lines), published.committed, published.preserved, receipt)
    return _deliver(home, published, git_state(home, runner=runner, fetch=False), lines, runner, receipt)


def clone(home: Path, url: str, *, force: bool = False, runner: Optional[GitRunner] = None) -> Outcome:
    """Clone a memory repository into ``home``; a non-empty ``home`` is refused unless ``force``.

    With ``force`` the existing directory is moved aside to a timestamped
    sibling first and moved back if the clone fails.
    """
    home = _home(home)
    if not _is_non_empty(home):
        home.parent.mkdir(parents=True, exist_ok=True)
        result = _git(home.parent, ["clone", url, str(home)], runner, timeout=NETWORK_TIMEOUT_SECONDS)
        if not result.ok:
            raise SyncError(f"git clone failed: {result.output}")
        return Outcome("cloned", True, (f"Cloned the memory repository into {home}.",))
    if not force:
        raise SyncError(f"refusing to clone into a non-empty home: {home}; pass --force to move it aside first")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = home.with_name(f"{home.name}.backup-{stamp}")
    shutil.move(str(home), str(backup))
    try:
        result = _git(home.parent, ["clone", url, str(home)], runner, timeout=NETWORK_TIMEOUT_SECONDS)
    except Exception:
        if not home.exists():
            shutil.move(str(backup), str(home))
        raise
    if not result.ok:
        if home.exists():
            shutil.rmtree(home)
        shutil.move(str(backup), str(home))
        raise SyncError(f"git clone failed: {result.output}")
    return Outcome(
        "cloned",
        True,
        (f"Moved the existing home aside to {backup}.", f"Cloned the memory repository into {home}."),
    )


def pull(home: Path, *, runner: Optional[GitRunner] = None) -> Outcome:
    """Fast-forward ``home`` from its upstream when that is the only thing that could happen.

    Silent on a home that is not configured for sync. Every state that rules
    a fast-forward out (a failed fetch, a dirty tree, divergence) is its own
    status and not a success; being ahead or having no upstream is reported
    and is not an error.
    """
    home = _home(home)
    if not is_configured(home):
        return Outcome("not_configured", True)
    _require_repo(home, runner)
    state = git_state(home, runner=runner, fetch=True)
    if not state.has_remote:
        return Outcome("local_only", True, ("memory pull: local-only repository; no remote to pull from.",))
    if state.fetch_failed:
        return _fetch_failure(state, "memory pull")
    if state.dirty:
        return Outcome("dirty", False, ("memory pull: uncommitted changes present; not pulling.",))
    if state.diverged:
        return Outcome("diverged", False, ("memory pull: diverged from upstream; resolve manually.",))
    if not state.has_upstream:
        return Outcome("no_upstream", True, ("memory pull: no upstream branch configured; skipping.",))
    if state.ahead:
        return Outcome("ahead", True, (f"memory pull: {state.ahead} unpushed commit(s); nothing to pull.",))
    if not state.behind:
        return Outcome("up_to_date", True, ("memory pull: already synced.",))
    result = _git(home, ["pull", "--ff-only", "--quiet"], runner, timeout=NETWORK_TIMEOUT_SECONDS)
    if result.timed_out:
        return Outcome("pull_timed_out", False, (f"memory pull: {result.output}",))
    if not result.ok:
        return Outcome("pull_failed", False, (f"memory pull: fast-forward failed: {result.output}",))
    return Outcome("synced", True, (f"memory pull: synced {state.behind} commit(s).",))


def push(
    home: Path,
    *,
    agent: Optional[str] = None,
    runner: Optional[GitRunner] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Outcome:
    """Commit the allowlisted memory changes and deliver them when a remote exists.

    Silent on a home that is not configured for sync. A home that is behind
    or diverged is refused before anything is committed. A commit that could
    not be delivered -- no remote, an unreachable one, a rejected push -- is
    reported as such, distinctly from delivery; a credential helper a sandbox
    blocked is named as its own cause, not as a rejection.
    """
    home = _home(home)
    if not is_configured(home):
        return Outcome("not_configured", True)
    _require_repo(home, runner)
    state = git_state(home, runner=runner, fetch=True)
    if state.has_remote and not state.fetch_failed:
        if state.diverged:
            return Outcome("diverged", False, ("memory push: diverged from upstream; resolve manually before pushing.",))
        if state.behind:
            return Outcome(
                "behind", False, (f"memory push: behind upstream by {state.behind} commit(s); pull before pushing.",)
            )
    published = _publish(home, agent=agent, runner=runner, env=env)
    if not published.ok:
        return published
    lines = list(published.lines)
    if not state.has_remote:
        lines.append("memory push: no remote configured; the commit remains local.")
        return Outcome("local_only", True, tuple(lines), published.committed, published.preserved)
    return _deliver(home, published, state, lines, runner)


def disable(home: Path) -> Outcome:
    """Remove the marker; the repository and its history stay in place."""
    home = _home(home)
    path = marker_path(home)
    if path.exists():
        path.unlink()
        return Outcome("disabled", True, (f"Removed {path}; syncing is disabled for this home.",))
    return Outcome("already_disabled", True, ("Syncing is already disabled for this home.",))


# --- the publication boundary -----------------------------------------------


def select_paths(home: Path, runner: Optional[GitRunner] = None) -> Tuple[List[str], List[str]]:
    """Split what ``git status`` reports inside the allowlisted tiers into (selected, outside).

    The query is limited to the layout's tier patterns plus the ignore file
    and the marker, so nothing elsewhere in the tree is ever a candidate; and
    every candidate is checked against :func:`layout.is_allowed_memory_path`,
    so a widened ignore file cannot smuggle a tier's unsynced subdirectory in.
    """
    pathspecs = [GITIGNORE_FILE, MARKER_FILE, *(f":(glob){tier.pattern}/**" for tier in layout.TIERS)]
    paths = _status_paths(home, runner, pathspecs)
    selected = sorted(path for path in paths if layout.is_allowed_memory_path(path))
    outside = sorted(path for path in paths if not layout.is_allowed_memory_path(path))
    return selected, outside


def changed_paths(home: Path, runner: Optional[GitRunner] = None) -> List[str]:
    """Every path ``git status`` reports anywhere in the tree, untracked files one by one.

    The whole-tree readout behind :attr:`GitState.dirty`, as paths: what ``pull``
    refuses to fast-forward over, whether or not the allowlist selects it.
    """
    return _status_paths(home, runner)


def staged_paths(home: Path, runner: Optional[GitRunner] = None) -> List[str]:
    """Every path the index differs in from HEAD (or from the empty tree before the first commit)."""
    result = _git(home, ["diff", "--cached", "--name-only", "-z"], runner)
    if not result.ok:
        raise SyncError(f"git diff --cached failed: {result.output}")
    return [path for path in result.stdout.split("\0") if path]


def committed_paths(home: Path, runner: Optional[GitRunner] = None) -> List[str]:
    """The paths HEAD's commit touched."""
    result = _git(home, ["show", "--format=", "--name-only", "-z", "HEAD"], runner)
    if not result.ok:
        raise SyncError(f"git show failed: {result.output}")
    return [path for path in result.stdout.split("\0") if path]


def resolve_agent(explicit: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> str:
    """The name a commit is published under: the caller's, else the environment's, else ``unknown``."""
    if explicit:
        return explicit
    source: Mapping[str, str] = os.environ if env is None else env
    for name in (ENV_AGENT, *_AGENT_FALLBACK_ENV):
        value = source.get(name)
        if value:
            return value
    return UNKNOWN_AGENT


def commit_message(file_count: int, agent: str, *, now: Optional[dt.datetime] = None) -> str:
    host = socket.gethostname().split(".")[0] or "host"
    today = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d")
    return f"memory: {agent}@{host} {today} ({file_count} files)"


def _publish(
    home: Path,
    *,
    agent: Optional[str],
    runner: Optional[GitRunner],
    env: Optional[Mapping[str, str]],
) -> Outcome:
    selected, outside = select_paths(home, runner)
    if outside:
        return Outcome(
            "outside_allowlist",
            False,
            (
                f"memory publish: refusing to commit; {len(outside)} path(s) inside a memory tier fall outside "
                f"the sync allowlist (a never-synced name such as keys/, or a widened {GITIGNORE_FILE}): "
                f"{', '.join(outside)}",
            ),
        )
    preserved = tuple(sorted(set(staged_paths(home, runner)) - set(selected)))
    lines: List[str] = []
    if preserved:
        lines.append(
            f"memory publish: {len(preserved)} staged path(s) outside the allowlist left staged and "
            f"uncommitted: {', '.join(preserved)}"
        )
    if not selected:
        lines.append("memory publish: no memory changes to commit.")
        return Outcome("nothing_to_commit", True, tuple(lines), (), preserved)

    message = commit_message(len(selected), resolve_agent(agent, env))
    with _pathspec_file(selected) as pathspec:
        added = _git(home, ["--literal-pathspecs", "add", *_from_file(pathspec)], runner)
        if not added.ok:
            return Outcome("commit_failed", False, (*lines, f"memory publish: git add failed: {added.output}"))
        committed = _git(home, ["--literal-pathspecs", "commit", "--quiet", "-m", message, *_from_file(pathspec)], runner)
    if not committed.ok:
        return Outcome("commit_failed", False, (*lines, f"memory publish: git commit failed: {committed.output}"))
    published = committed_paths(home, runner)
    stray = sorted(path for path in published if not layout.is_allowed_memory_path(path))
    if stray:
        raise SyncError(f"memory publish: HEAD commits paths outside the allowlist: {', '.join(stray)}")
    lines.append(f"memory publish: committed {len(published)} file(s).")
    return Outcome("committed", True, tuple(lines), tuple(published), preserved)


def _deliver(
    home: Path,
    published: Outcome,
    state: GitState,
    lines: List[str],
    runner: Optional[GitRunner],
    receipt: Optional[GitignoreReceipt] = None,
) -> Outcome:
    if state.fetch_failed:
        if _credential_helper_blocked(state.fetch_output):
            return _helper_blocked(lines, published, receipt)
        failure = _fetch_failure(state, "memory push")
        lines.append(f"{failure.lines[0]} The commit remains local.")
        return Outcome(failure.status, False, tuple(lines), published.committed, published.preserved, receipt)
    if not published.committed and state.has_upstream and not state.ahead:
        lines.append("memory push: remote already up to date.")
        return Outcome("up_to_date", True, tuple(lines), (), published.preserved, receipt)
    result = _push_remote(home, state, runner)
    if not result.ok:
        if _credential_helper_blocked(result.output):
            return _helper_blocked(lines, published, receipt)
        what = "timed out" if result.timed_out else "was rejected"
        lines.append(f"memory push: the push {what}; the commit remains local. {result.output}".rstrip())
        status = "push_timed_out" if result.timed_out else "push_failed"
        return Outcome(status, False, tuple(lines), published.committed, published.preserved, receipt)
    lines.append("memory push: delivered to the remote.")
    return Outcome("pushed", True, tuple(lines), published.committed, published.preserved, receipt)


def _push_remote(home: Path, state: GitState, runner: Optional[GitRunner]) -> GitResult:
    if state.has_upstream:
        return _git(home, ["push", "--quiet"], runner, timeout=NETWORK_TIMEOUT_SECONDS)
    remote = _default_remote(home, runner)
    branch = _current_branch(home, runner)
    return _git(home, ["push", "--quiet", "-u", remote, branch], runner, timeout=NETWORK_TIMEOUT_SECONDS)


def _credential_helper_blocked(output: str) -> bool:
    """Does this git failure carry the blocked-credential-helper signature?

    git falls back to prompting when a helper hands it nothing, and under a
    sandbox that prompt has no terminal to read from either. Both halves are
    required, and the second must come from the helper: a prompt failure on
    its own is what any host without usable credentials looks like, down to
    the errno text macOS appends to it.
    """
    lowered = output.lower()
    if not any(prompt in lowered for prompt in _CREDENTIAL_PROMPT_FAILURES):
        return False
    return any(tell in lowered for tell in _SANDBOXED_HELPER_TELLS)


def _helper_blocked(
    lines: List[str], published: Outcome, receipt: Optional[GitignoreReceipt]
) -> Outcome:
    lines.append(_HELPER_BLOCKED_LINE)
    return Outcome(
        "credential_helper_blocked", False, tuple(lines), published.committed, published.preserved, receipt
    )


def _fetch_failure(state: GitState, verb: str) -> Outcome:
    if state.fetch_timed_out:
        return Outcome("fetch_timed_out", False, (f"{verb}: the fetch timed out; {state.fetch_output}".rstrip(),))
    return Outcome("fetch_failed", False, (f"{verb}: the fetch failed; {state.fetch_output}".rstrip(),))


# --- helpers ----------------------------------------------------------------


def _git(
    cwd: Path, args: Sequence[str], runner: Optional[GitRunner], *, timeout: Optional[float] = None
) -> GitResult:
    return (runner or run_git)(args, cwd=cwd, timeout=timeout)


def _home(value: Path) -> Path:
    return Path(value).expanduser().absolute()


def _require_repo(home: Path, runner: Optional[GitRunner]) -> None:
    if not is_git_repo(home, runner):
        raise SyncError(f"{MARKER_FILE} is present, but {home} is not a git repository")
    _require_root(home, runner)


def _require_root(home: Path, runner: Optional[GitRunner]) -> None:
    root = worktree_root(home, runner)
    if root is None:
        raise SyncError(f"{home} is not inside a git worktree")
    if root.resolve() != home.resolve():
        raise SyncError(
            f"root mismatch: {home} is inside the git worktree {root}; "
            "a memory home must be the root of its own repository"
        )


def _status_paths(home: Path, runner: Optional[GitRunner], pathspecs: Sequence[str] = ()) -> List[str]:
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    if pathspecs:
        args += ["--", *pathspecs]
    result = _git(home, args, runner)
    if not result.ok:
        raise SyncError(f"git status failed: {result.output}")
    return _parse_status_z(result.stdout)


def _status_porcelain(home: Path, runner: Optional[GitRunner]) -> str:
    result = _git(home, ["status", "--porcelain"], runner)
    if not result.ok:
        raise SyncError(f"git status failed: {result.output}")
    return result.stdout.strip()


def _has_remote(home: Path, runner: Optional[GitRunner]) -> bool:
    result = _git(home, ["remote"], runner)
    return result.ok and bool(result.stdout.strip())


def _default_remote(home: Path, runner: Optional[GitRunner]) -> str:
    result = _git(home, ["remote"], runner)
    remotes = [line.strip() for line in result.stdout.splitlines() if line.strip()] if result.ok else []
    return remotes[0] if remotes else DEFAULT_REMOTE


def _upstream(home: Path, runner: Optional[GitRunner]) -> str:
    result = _git(home, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], runner)
    return result.stdout.strip() if result.ok else ""


def _current_branch(home: Path, runner: Optional[GitRunner]) -> str:
    result = _git(home, ["branch", "--show-current"], runner)
    return result.stdout.strip() if result.ok and result.stdout.strip() else "HEAD"


def _is_non_empty(path: Path) -> bool:
    return path.exists() and any(path.iterdir())


def _parse_status_z(data: str) -> List[str]:
    """Paths from ``git status --porcelain=v1 -z``; a rename or copy contributes both of its paths.

    The ``R``/``C`` marker sits in the index column for a staged move and in the
    worktree column for one git detects in the tree (an intent-to-add move); either
    way the record carries the source path as the next NUL-separated field.
    """
    paths: List[str] = []
    fields = data.split("\0")
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if (entry[0] in "RC" or entry[1] in "RC") and index < len(fields) and fields[index]:
            paths.append(fields[index])
            index += 1
    return paths


def _from_file(pathspec: Path) -> List[str]:
    return [f"--pathspec-from-file={pathspec}", "--pathspec-file-nul"]


class _pathspec_file:
    """A NUL-separated pathspec file for ``--pathspec-from-file``, removed on exit."""

    def __init__(self, paths: Iterable[str]) -> None:
        self._paths = list(paths)
        self._path: Optional[Path] = None

    def __enter__(self) -> Path:
        handle = tempfile.NamedTemporaryFile("wb", prefix="agent-memory-pathspec-", delete=False)
        with handle:
            handle.write("\0".join(self._paths).encode("utf-8", "surrogateescape"))
        self._path = Path(handle.name)
        return self._path

    def __exit__(self, *_: object) -> None:
        if self._path is not None:
            try:
                self._path.unlink()
            except OSError:
                pass


def _with(outcome: Outcome, *, lines: Sequence[str], receipt: Optional[GitignoreReceipt]) -> Outcome:
    return Outcome(outcome.status, outcome.ok, tuple(lines), outcome.committed, outcome.preserved, receipt)


def _indent(lines: Iterable[str]) -> List[str]:
    return [f"    {line}" for line in lines]
