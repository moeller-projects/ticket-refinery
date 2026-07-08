"""Concurrent shallow clone with short-lived PAT credentials."""
import base64
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def clone_all(repos: list[dict], workspace: Path, depth: int, pat: str | None) -> None:
    """Clone each repo into <workspace>/<repo[name]>, shallow, in parallel."""
    workspace.mkdir(parents=True, exist_ok=True)
    if not repos:
        return
    with ThreadPoolExecutor(max_workers=min(len(repos), 4)) as ex:
        list(ex.map(lambda r: _clone_one(r, workspace, depth, pat), repos))


def _clone_one(repo: dict, workspace: Path, depth: int, pat: str | None) -> None:
    target = workspace / repo["name"]
    if (target / ".git").exists():
        return
    env = os.environ.copy()
    if pat:
        # ponytail: per-clone credential header via GIT_CONFIG_* env vars.
        # Avoids baking PAT into the remote URL (process-list leak).
        header = base64.b64encode(f":{pat}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {header}"
    cmd = [
        "git", "clone",
        "--depth", str(depth),
        "--branch", repo["defaultBranch"],
        repo["url"], str(target),
    ]
    subprocess.run(cmd, check=True, env=env)


def cleanup(workspace: Path) -> None:
    shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":  # ponytail: one-shot smoke check (needs git on PATH + a reachable repo)
    import sys, tempfile
    if len(sys.argv) < 2:
        print("usage: python git_ops.py <url> [branch]"); sys.exit(2)
    url, branch = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "main"
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        clone_all([{"name": "t", "url": url, "defaultBranch": branch}], ws, depth=1, pat=None)
        assert (ws / "t" / ".git").exists()
        print("clone ok")