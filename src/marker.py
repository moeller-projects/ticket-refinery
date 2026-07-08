"""Idempotency hash: title + description + sorted repo HEAD SHAs."""
import hashlib
import subprocess
from pathlib import Path


def _head_sha(repo_dir: Path) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def compute(item: dict, workspace: Path) -> str:
    """sha1(item.title + item.description + sorted repo head SHAs)."""
    fields = item.get("fields", {})
    parts = [
        fields.get("System.Title", ""),
        fields.get("System.Description", "") or "",
    ]
    # ponytail: globbing for .git dirs assumes <workspace>/<repo>/.git layout.
    # Switch to explicit repo list when sparse clones or monorepos appear.
    head_shas = []
    for git_dir in sorted(workspace.glob("*/.git")):
        try:
            head_shas.append(_head_sha(git_dir.parent))
        except subprocess.CalledProcessError:
            continue
    parts.append("".join(sorted(head_shas)))
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


if __name__ == "__main__":  # ponytail: one-shot self-check
    sample = {"fields": {"System.Title": "t", "System.Description": "d"}}
    ws = Path("/tmp")
    h = compute(sample, ws)
    assert len(h) == 40 and all(c in "0123456789abcdef" for c in h)
    print("marker ok:", h)