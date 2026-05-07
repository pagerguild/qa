#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""qa: local runner for pagerguild QA agents.

Bootstrap (no install needed):

    uv run https://raw.githubusercontent.com/pagerguild/qa/main/qa.py \\
      --target http://localhost:5173 --here

Discovers .qa/<agent>/ task folders in a target repo, runs them in
parallel matrix containers via act using the same qa-matrix.yml
workflow as production CI, and streams each agent's progress into
the team's Supabase reader.

Prereqs on the host: uv, gh (authed), doppler (with qa-team scope),
act, and Docker. On Apple Silicon, native arm64 containers are
selected automatically.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

VERSION = "0.1.0"
CACHE_ROOT = Path.home() / ".cache" / "pagerguild-qa"
QA_TEAM_REPO = "pagerguild/qa-team"
QA_TEAM_CACHE = CACHE_ROOT / "qa-team"
RUNS_ROOT = CACHE_ROOT / "runs"
RUNNER_IMAGE_LOCAL = "pagerguild/qa-runner:local"
RUNNER_IMAGE_REGISTRY = "ghcr.io/pagerguild/qa-runner:latest"  # populated when CI publishes


# ---------------------------------------------------------------------------
# Pretty-printing
# ---------------------------------------------------------------------------

def info(msg: str) -> None:
    print(f"\033[36m›\033[0m {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"\033[33m!\033[0m {msg}", file=sys.stderr, flush=True)


def fatal(msg: str, code: int = 1) -> "None":
    print(f"\033[31m✗\033[0m {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

def need(cmd: str, hint: str) -> None:
    if shutil.which(cmd) is None:
        fatal(f"{cmd!r} not found on PATH. {hint}")


def pre_flight() -> None:
    need("gh", "Install with `brew install gh` and `gh auth login`.")
    need("git", "Install Xcode CLT or `brew install git`.")
    need("doppler", "Install with `brew install dopplerhq/cli/doppler` and `doppler login`.")
    need("act", "Install with `brew install act`.")
    need("docker", "Install Docker Desktop and ensure it's running.")
    proc = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if proc.returncode != 0:
        fatal("Docker daemon is not reachable. Start Docker Desktop and retry.")


# ---------------------------------------------------------------------------
# Doppler bridge — mint a 1-hour service token from the user's CLI session
# ---------------------------------------------------------------------------

@dataclass
class DopplerToken:
    name: str
    token: str
    project: str
    config: str

    def revoke(self) -> None:
        # `revoke [slug|token]` — pass the token itself (we don't capture
        # the slug at create time). Idempotent enough for cleanup.
        proc = subprocess.run(
            ["doppler", "configs", "tokens", "revoke", self.token,
             "--project", self.project, "--config", self.config],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            warn(f"failed to revoke Doppler token {self.name}: "
                 f"{proc.stderr.strip() or proc.stdout.strip()}")


def mint_doppler_token() -> DopplerToken:
    project = subprocess.run(
        ["doppler", "configure", "get", "project", "--plain"],
        capture_output=True, text=True,
    ).stdout.strip()
    config = subprocess.run(
        ["doppler", "configure", "get", "config", "--plain"],
        capture_output=True, text=True,
    ).stdout.strip()
    if not project or not config:
        fatal("No Doppler scope configured for this directory. "
              "Run `doppler setup` from a directory with a .doppler.yaml, "
              "or `doppler configure set project=qa-team config=prd --scope $PWD`.")
    name = f"qa-local-{int(time.time())}"
    proc = subprocess.run(
        ["doppler", "configs", "tokens", "create", name,
         "--project", project, "--config", config,
         "--max-age", "1h", "--plain"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        fatal("Failed to mint Doppler service token: "
              f"{proc.stderr.strip() or proc.stdout.strip()}")
    info(f"Minted Doppler service token {name} (project={project}, config={config}, max-age=1h)")
    return DopplerToken(name=name, token=proc.stdout.strip(), project=project, config=config)


# ---------------------------------------------------------------------------
# qa-team checkout — always-latest from main, cached at ~/.cache/pagerguild-qa
# ---------------------------------------------------------------------------

def ensure_qa_team_cache() -> Path:
    QA_TEAM_CACHE.parent.mkdir(parents=True, exist_ok=True)
    if not QA_TEAM_CACHE.exists():
        info(f"Cloning {QA_TEAM_REPO} → {QA_TEAM_CACHE}")
        subprocess.run(
            ["gh", "repo", "clone", QA_TEAM_REPO, str(QA_TEAM_CACHE), "--", "--depth", "1"],
            check=True,
        )
    else:
        info(f"Refreshing {QA_TEAM_REPO} cache")
        subprocess.run(["git", "-C", str(QA_TEAM_CACHE), "fetch", "--depth", "1", "origin", "main"], check=True)
        subprocess.run(["git", "-C", str(QA_TEAM_CACHE), "reset", "--hard", "origin/main"], check=True)
    return QA_TEAM_CACHE


# ---------------------------------------------------------------------------
# Target resolution — figure out which repo to test and at what SHA
# ---------------------------------------------------------------------------

@dataclass
class Target:
    workspace: Path        # local dir containing .qa/ and (for git repos) a .git/
    owner: str
    name: str
    sha: str
    ref: str
    cleanup_dir: Optional[Path] = None  # set when we cloned to a temp dir

    def cleanup(self) -> None:
        if self.cleanup_dir and self.cleanup_dir.exists():
            shutil.rmtree(self.cleanup_dir, ignore_errors=True)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _origin_owner_name(cwd: Path) -> tuple[str, str]:
    url = _git("remote", "get-url", "origin", cwd=cwd)
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$", url)
    if not m:
        fatal(f"Cannot parse owner/name from origin url: {url}")
    return m.group(1), m.group(2)


def resolve_target(args: argparse.Namespace) -> Target:
    if args.repo:
        owner_name = args.repo
        if "/" not in owner_name:
            fatal("--repo must be OWNER/NAME")
        owner, name = owner_name.split("/", 1)
        tmp = Path(tempfile.mkdtemp(prefix="qa-target-", dir=str(CACHE_ROOT)))
        info(f"Cloning {owner}/{name}@{args.branch} → {tmp}")
        clone_args = ["gh", "repo", "clone", owner_name, str(tmp), "--", "--depth", "1"]
        if args.branch:
            clone_args[4:4] = []  # no-op; branch goes to git clone via --
            clone_args = ["gh", "repo", "clone", owner_name, str(tmp), "--",
                          "--depth", "1", "--branch", args.branch]
        subprocess.run(clone_args, check=True)
        sha = _git("rev-parse", "HEAD", cwd=tmp)
        ref = f"refs/heads/{args.branch}" if args.branch else _git(
            "symbolic-ref", "--short", "HEAD", cwd=tmp)
        return Target(workspace=tmp, owner=owner, name=name, sha=sha,
                      ref=f"refs/heads/{ref}" if not ref.startswith("refs/") else ref,
                      cleanup_dir=tmp)
    workspace = Path(args.path or os.getcwd()).resolve()
    if not (workspace / ".git").exists():
        fatal(f"{workspace} is not a git repository (no .git/). "
              "Use --repo OWNER/NAME if you don't have a local checkout.")
    owner, name = _origin_owner_name(workspace)
    sha = _git("rev-parse", "HEAD", cwd=workspace)
    try:
        ref = _git("symbolic-ref", "HEAD", cwd=workspace)
    except subprocess.CalledProcessError:
        ref = "refs/heads/HEAD"
    return Target(workspace=workspace, owner=owner, name=name, sha=sha, ref=ref)


# ---------------------------------------------------------------------------
# Workspace prep — overlay qa-team's scripts/.github onto target's .qa/
# ---------------------------------------------------------------------------

def prepare_workspace(qa_team: Path, target: Target, qa_dir_name: str) -> Path:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"run-{int(time.time())}-", dir=str(RUNS_ROOT)))
    # Copy the target's .git first so `git rev-parse HEAD` in the workspace
    # returns the target's SHA. act computes its own GITHUB_SHA from the
    # workspace's HEAD and overrides --env passthrough — keeping the target's
    # .git is the only way to make GITHUB_SHA propagate correctly through act.
    shutil.copytree(target.workspace / ".git", work / ".git")
    # Overlay qa-team's scripts + workflows on top.
    shutil.copytree(qa_team / "scripts", work / "scripts")
    shutil.copytree(qa_team / ".github", work / ".github")
    # And the target's .qa task definitions.
    qa_dir_src = target.workspace / qa_dir_name
    if not qa_dir_src.exists():
        fatal(f"Target has no {qa_dir_name}/ directory at {qa_dir_src}")
    shutil.copytree(qa_dir_src, work / ".qa")
    return work


# ---------------------------------------------------------------------------
# Localhost rewrite — make `localhost` reachable from inside the act container
# ---------------------------------------------------------------------------

LOCALHOST_RE = re.compile(r"^(https?://)(localhost|127\.0\.0\.1|0\.0\.0\.0)(:|/|$)")


def rewrite_localhost(url: str) -> str:
    rewritten = LOCALHOST_RE.sub(r"\1host.docker.internal\3", url)
    if rewritten != url:
        info(f"Rewriting --target {url} → {rewritten} (host.docker.internal)")
    return rewritten


# ---------------------------------------------------------------------------
# act invocation
# ---------------------------------------------------------------------------

def detect_arch_flag() -> Optional[str]:
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "linux/arm64"
    if machine in ("x86_64", "amd64"):
        return "linux/amd64"
    warn(f"Unrecognized host arch {machine!r}; letting act pick its default.")
    return None


def ensure_runner_image() -> str:
    """Return the Docker image act should use as the ubuntu-latest runner.

    Resolution order:
      1. Already cached locally as RUNNER_IMAGE_LOCAL → use it.
      2. Pull RUNNER_IMAGE_REGISTRY from GHCR, retag as RUNNER_IMAGE_LOCAL.
      3. Dev fallback: build from a sibling Dockerfile (only present when
         running from a qa-cli checkout, not when bootstrapped via `uv run`).
    """
    inspect = subprocess.run(
        ["docker", "image", "inspect", RUNNER_IMAGE_LOCAL],
        capture_output=True,
    )
    if inspect.returncode == 0:
        info(f"Using cached runner image {RUNNER_IMAGE_LOCAL}")
        return RUNNER_IMAGE_LOCAL

    info(f"Pulling runner image from {RUNNER_IMAGE_REGISTRY}…")
    pull = subprocess.run(["docker", "pull", RUNNER_IMAGE_REGISTRY])
    if pull.returncode == 0:
        subprocess.run(
            ["docker", "tag", RUNNER_IMAGE_REGISTRY, RUNNER_IMAGE_LOCAL],
            check=True,
        )
        return RUNNER_IMAGE_LOCAL

    dockerfile = Path(__file__).resolve().parent / "Dockerfile"
    if dockerfile.exists():
        info(f"Pull failed; building runner image locally from {dockerfile} (~3 min)…")
        proc = subprocess.run(
            ["docker", "build", "-t", RUNNER_IMAGE_LOCAL,
             "-f", str(dockerfile), str(dockerfile.parent)],
        )
        if proc.returncode != 0:
            fatal("docker build failed; see output above")
        return RUNNER_IMAGE_LOCAL

    fatal(
        "Could not obtain runner image. Tried:\n"
        f"  • local cache: docker image inspect {RUNNER_IMAGE_LOCAL}\n"
        f"  • registry:    docker pull {RUNNER_IMAGE_REGISTRY}\n"
        f"  • local build: {dockerfile} (not found — uv-run scripts don't fetch siblings)\n"
        "Check Docker is running and that you can reach ghcr.io."
    )
    return ""  # unreachable; appeases type checker


def run_act(work: Path, target: Target, target_url: str,
            doppler_token: DopplerToken, args: argparse.Namespace) -> int:
    # Write a one-off secrets file (chmod 600), unlinked in finally.
    secrets_path = work / ".secrets"
    secrets_path.write_text(f"DOPPLER_TOKEN={doppler_token.token}\n")
    secrets_path.chmod(0o600)

    arch = detect_arch_flag()
    run_id = str(int(time.time() * 1000))
    runner_image = ensure_runner_image()

    cmd = [
        "act", "workflow_dispatch",
        "-W", ".github/workflows/test-action.yml",
        "-P", f"ubuntu-latest={runner_image}",
        "--pull=false",
        "--secret-file", str(secrets_path),
        "--input", f"target_url={target_url}",
        "--input", "qa_dir=.qa",
        "--env", f"GITHUB_REPOSITORY={target.owner}/{target.name}",
        "--env", f"GITHUB_SHA={target.sha}",
        "--env", f"GITHUB_REF={target.ref}",
        "--env", f"GITHUB_RUN_ID={run_id}",
        "--env", "GITHUB_EVENT_NAME=workflow_dispatch",
        "--env", f"GITHUB_ACTOR={os.environ.get('USER', 'qa-local')}",
        "--container-options", "--add-host=host.docker.internal:host-gateway",
    ]
    if arch:
        cmd += ["--container-architecture", arch]
    if args.verbose:
        cmd.append("--verbose")

    info(f"Running act in {work} (arch={arch or 'default'})")
    info(f"  GITHUB_REPOSITORY={target.owner}/{target.name}")
    info(f"  GITHUB_SHA={target.sha[:12]}")
    info(f"  target-url={target_url}")
    print(f"\033[2m$ cd {work} && {' '.join(cmd)}\033[0m", flush=True)

    proc = subprocess.run(cmd, cwd=str(work))
    return proc.returncode


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="qa",
        description="Local runner for pagerguild QA agents (act-driven).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              qa --target http://localhost:5173 --here
              qa --target https://staging.example.com --path ~/src/some-app
              qa --target https://staging.example.com --repo pagerguild/foo --branch fix-login
        """),
    )
    p.add_argument("--target", required=True,
                   help="Base URL the agents will exercise (e.g. http://localhost:5173).")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--here", action="store_true",
                     help="Use the current working directory as the target repo.")
    src.add_argument("--path", help="Path to a local checkout of the target repo.")
    src.add_argument("--repo",
                     help="OWNER/NAME of a github repo to clone fresh into a temp dir.")
    p.add_argument("--branch",
                   help="Branch to clone when --repo is used (default: repo default).")
    p.add_argument("--qa-dir", default=".qa",
                   help="Directory containing .qa/<agent>/ folders (default: .qa).")
    p.add_argument("--verbose", action="store_true", help="Pass --verbose to act.")
    p.add_argument("--version", action="version", version=f"qa {VERSION}")
    args = p.parse_args()
    if args.here:
        args.path = os.getcwd()
    return args


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    pre_flight()
    qa_team = ensure_qa_team_cache()
    target = resolve_target(args)
    target_url = rewrite_localhost(args.target)
    doppler = mint_doppler_token()

    @atexit.register
    def _cleanup() -> None:
        with contextlib.suppress(Exception):
            doppler.revoke()
        with contextlib.suppress(Exception):
            target.cleanup()

    work = prepare_workspace(qa_team, target, args.qa_dir)

    def _on_signal(signum: int, _frame) -> None:
        warn(f"received signal {signum}, cleaning up")
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        return run_act(work, target, target_url, doppler, args)
    finally:
        # Keep the workspace dir for post-mortem; runs/ accretes but it's
        # under ~/.cache so it's safe to delete by hand.
        info(f"Run workspace: {work}")


if __name__ == "__main__":
    sys.exit(main())
