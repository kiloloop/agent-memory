#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Prove the built wheel operates a memory home with nothing else installed.

Run ``make build`` first. Each step is fatal on failure:

1. create a throwaway venv and install the newest ``dist/*.whl`` plus pytest, no extras;
2. assert the installed distribution declares no runtime dependencies and that the
   kernel package this tool is leaving is not importable in that venv;
3. with AGENT_MEMORY_HOME and OACP_HOME stripped from the environment, scaffold a fresh
   home in a temp dir with the installed package, run ``agent-memory status --home`` and
   ``agent-memory doctor --home`` on it, scaffold again, and assert no byte changed; then
   ``agent-memory init --project demo --repo <dir>`` a second home: its org-tier files must
   equal this checkout's templates byte for byte (the templates travelled inside the wheel),
   the binding must exist, and rerunning ``init`` and ``org init`` must change no byte;
   then ``agent-memory setup claude`` on that repository must write the executable hook
   script, register exactly it, write a receipt in the home and change no byte on a rerun,
   and ``agent-memory startup --json`` must list the seven tier files as readable;
4. optionally run ``status`` and ``doctor`` against a live home (``--home``); each may
   exit 0 or 1 there (a dirty tree, an error row), never anything else;
5. run this checkout's test suite with AGENT_MEMORY_TEST_INSTALLED=1, so the import must
   come from site-packages and the console script must be on PATH.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
DISTRIBUTION = "agent-memory-cli"
STRIPPED_ENV = ("AGENT_MEMORY_HOME", "OACP_HOME", "PYTHONPATH")
SCAFFOLD = (
    "import sys; from pathlib import Path; from agent_memory.layout import scaffold_home; "
    "print(len(scaffold_home(Path(sys.argv[1]))))"
)
ORG_FILES = ("recent.md", "decisions.md", "rules.md")
PROJECT_FILES = ("project_facts.md", "decision_log.md", "open_threads.md", "known_debt.md")


def newest_wheel() -> Path:
    wheels = sorted(DIST.glob("agent_memory_cli-*.whl"), key=lambda path: path.stat().st_mtime)
    if not wheels:
        sys.exit("no wheel in dist/: run `make build` first")
    return wheels[-1]


def venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def run(
    argv: Sequence[object],
    env: Mapping[str, str],
    *,
    cwd: Optional[Path] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    command = [str(item) for item in argv]
    print("+", " ".join(command), flush=True)
    result = subprocess.run(command, env=dict(env), cwd=cwd, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.exit(f"command failed with exit {result.returncode}: {' '.join(command)}")
    return result


def tree_digest(root: Path) -> Dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", help="also run `agent-memory status --home PATH` against this live home")
    parser.add_argument("--keep", action="store_true", help="keep the throwaway venv and home; print their dir")
    args = parser.parse_args(argv)

    wheel = newest_wheel()
    workdir = Path(tempfile.mkdtemp(prefix="agent-memory-wheel-check-"))
    venv_dir = workdir / "venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    python = venv_python(venv_dir)
    env = {key: value for key, value in os.environ.items() if key not in STRIPPED_ENV}
    env["PATH"] = os.pathsep.join([str(python.parent), env.get("PATH", "")])
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

    run([python, "-m", "pip", "install", "--quiet", wheel, "pytest"], env)

    probe = run(
        [python, "-c", f"import importlib.metadata as m, json; print(json.dumps(m.requires({DISTRIBUTION!r}) or []))"],
        env,
    )
    runtime = [entry for entry in json.loads(probe.stdout) if "extra ==" not in entry]
    if runtime:
        sys.exit(f"the wheel declares runtime dependencies: {runtime}")
    foreign = run(
        [python, "-c", "import importlib.util as u; raise SystemExit(0 if u.find_spec('oacp') is None else 1)"],
        env,
        check=False,
    )
    if foreign.returncode != 0:
        sys.exit("the kernel package is importable inside the throwaway venv; the proof would be void")

    fresh = workdir / "home"
    created = int(run([python, "-c", SCAFFOLD, fresh], env).stdout)
    if created == 0:
        sys.exit("scaffold created nothing in an empty directory")
    before = tree_digest(fresh)
    console = shutil.which("agent-memory", path=str(python.parent))
    if console is None:
        sys.exit("the wheel did not install the agent-memory console script")
    for verb in ("status", "doctor"):
        sys.stdout.write(run([console, verb, "--home", fresh], env).stdout)
    if tree_digest(fresh) != before:
        sys.exit("status or doctor changed the fresh home")
    recreated = int(run([python, "-c", SCAFFOLD, fresh], env).stdout)
    if recreated != 0 or tree_digest(fresh) != before:
        sys.exit("scaffolding the same home again changed it")

    second = workdir / "home2"
    repo = workdir / "repo"
    repo.mkdir()
    sys.stdout.write(run([console, "init", "--home", second, "--project", "demo", "--repo", repo], env).stdout)
    templates = ROOT / "src" / "agent_memory" / "templates"
    for tier_dir, names in (("org-memory", ORG_FILES), ("project-memory", PROJECT_FILES)):
        target = second / ("org-memory" if tier_dir == "org-memory" else "projects/demo/memory")
        for name in names:
            if (target / name).read_bytes() != (templates / tier_dir / name).read_bytes():
                sys.exit(f"{tier_dir}/{name} written by the installed wheel differs from the source template")
    if not (repo / ".agent-memory.json").is_file():
        sys.exit("init --repo recorded no binding")
    snapshot = tree_digest(second)
    run([console, "init", "--home", second, "--project", "demo", "--repo", repo], env)
    run([console, "org", "init", "--home", second], env)
    if tree_digest(second) != snapshot:
        sys.exit("rerunning init or org init changed the home")

    sys.stdout.write(run([console, "setup", "claude", "--repo", repo, "--home", second], env).stdout)
    hook = repo / ".claude" / "hooks" / "agent-memory-pull.sh"
    if not hook.is_file() or not os.access(hook, os.X_OK):
        sys.exit("setup claude wrote no executable hook script")
    if not list((second / "setup" / "claude").glob("*.json")):
        sys.exit("setup claude wrote no receipt in the home")
    settings = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
    commands = [entry["command"] for group in settings["hooks"]["SessionStart"] for entry in group["hooks"]]
    if commands != [".claude/hooks/agent-memory-pull.sh"]:
        sys.exit(f"setup claude registered {commands}")
    if "SessionEnd" in settings["hooks"]:
        sys.exit("setup claude registered a session-end hook")
    installed = (tree_digest(repo), tree_digest(second))
    run([console, "setup", "claude", "--repo", repo, "--home", second], env)
    if (tree_digest(repo), tree_digest(second)) != installed:
        sys.exit("rerunning setup claude changed a byte")
    manifest = json.loads(
        run([console, "startup", "--runtime", "claude", "--home", second, "--project", "demo", "--json"], env).stdout
    )
    states = [entry["state"] for entry in manifest["files"]]
    if manifest["schema_version"] != 1 or manifest["content_injected"] or states != ["readable"] * (len(PROJECT_FILES) + len(ORG_FILES)):
        sys.exit(f"the startup manifest from the installed wheel is wrong: {json.dumps(manifest)}")

    if args.home:
        for verb in ("status", "doctor"):
            live = run([console, verb, "--home", args.home], env, check=False)
            sys.stdout.write(live.stdout)
            if live.returncode not in (0, 1):
                sys.stderr.write(live.stderr)
                sys.exit(f"{verb} on the live home exited {live.returncode}; 0 or 1 are its only outcomes")

    tests = run(
        [python, "-m", "pytest", "-q", "tests"],
        {**env, "AGENT_MEMORY_TEST_INSTALLED": "1"},
        cwd=ROOT,
        check=False,
    )
    sys.stdout.write(tests.stdout)
    if tests.returncode != 0:
        sys.stderr.write(tests.stderr)
        return tests.returncode

    print(
        f"installed-wheel check OK: {wheel.name} on Python {sys.version.split()[0]}; "
        f"fresh home scaffolded {created} path(s), {len(before)} file(s), rerun preserved bytes"
    )
    if args.keep:
        print(f"kept: {workdir}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
