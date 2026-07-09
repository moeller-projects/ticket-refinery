"""Workspace lifecycle: clone, cache linking, cleanup.

Responsibilities:
- Resolve the per-item workspace path.
- Clone requested repos (with retry on transient git failures).
- Symlink each repo into the workspace from a shared cache root (clone-reuse
  across items in the same process).
- Clean the workspace on item completion or error.

Implementation note:
- `git_ops` is referenced as a module attribute (`git_ops.clone_all`,
  `git_ops.cleanup`) rather than via `from git_ops import …` so test
  monkeypatching (`monkeypatch.setattr(refine.git_ops, "clone_all", ...)`)
  propagates here.
"""

from __future__ import annotations

import logging
import shutil
import subprocess  # noqa: F401  (used by retryable tuple)
from dataclasses import dataclass
from pathlib import Path

import git_ops
from retry import with_retry

log = logging.getLogger("refine.workspace")


@dataclass(frozen=True)
class Workspace:
    """One scratch workspace for one item."""

    path: Path
    repo_names: tuple[str, ...]

    @property
    def repos(self) -> list[Path]:
        return [self.path / name for name in self.repo_names]


class WorkspaceService:
    """Owns the cache root + per-item workspace symlinks."""

    def __init__(self, *, cache_root: Path | None = None) -> None:
        self._cache_root = cache_root

    @property
    def cache_root(self) -> Path | None:
        return self._cache_root

    def prepare(
        self,
        item_id: int,
        repos: list[dict],
        depth: int,
        pat: str | None,
        *,
        on_clone_duration: callable | None = None,
    ) -> Workspace:
        """Clone repos (cached) and link them into a per-item workspace.

        `on_clone_duration(seconds)` is called with the clone wall-clock time
        so the caller can record metrics without WorkspaceService caring about
        the metrics abstraction.

        When no separate `cache_root` was provided to the constructor, the
        per-item workspace IS the cache (clone directly into it, no symlink).
        Pass a `cache_root` to share clones across items in the same run.
        """
        workspace = Path(f"/tmp/refine-{item_id}")
        cache_root = self._cache_root if self._cache_root is not None else workspace
        cache_root.mkdir(parents=True, exist_ok=True)
        log.info(
            "item %s repos=%s cache_root=%s workspace=%s",
            item_id,
            [r["name"] for r in repos],
            cache_root,
            workspace,
        )

        clone_started = _now()
        # ponytail: git failures (network, missing branch) are the only clone
        # errors worth retrying; missing-repo / auth errors propagate as
        # subprocess.CalledProcessError (NOT in retryable tuple when bad repo).
        with_retry(
            lambda: git_ops.clone_all(repos, cache_root, depth, pat),
            retryable=(
                subprocess.CalledProcessError,
                OSError,
                ConnectionError,
                TimeoutError,
            ),
        )
        if on_clone_duration is not None:
            on_clone_duration(_now() - clone_started)

        self._sync_graphify_indexes(repos, cache_root)

        if cache_root != workspace:
            self._link_repo_cache(repos, cache_root, workspace)
        for repo in repos:
            log.info(
                "item %s repo_ready name=%s path=%s git=%s",
                item_id,
                repo["name"],
                workspace / repo["name"],
                (workspace / repo["name"] / ".git").exists(),
            )
        return Workspace(path=workspace, repo_names=tuple(r["name"] for r in repos))

    def cleanup(self, workspace: Path) -> None:
        git_ops.cleanup(workspace)

    def shutdown(self) -> None:
        """Final cache cleanup when the run exits."""
        if self._cache_root is not None:
            git_ops.cleanup(self._cache_root)
            # Defensive: also drop any stale per-process cache.
            shutil.rmtree(self._cache_root, ignore_errors=True)

    @staticmethod
    def _sync_graphify_indexes(repos: list[dict], cache_root: Path) -> None:
        """Refresh Graphify index after clone. Safe no-op if CLI absent.

        Graphify writes a parsed AST graph at `<repo>/graphify-out/graph.json`
        via `graphify extract --code-only --no-cluster`. We run that with
        `--code-only` so no LLM API key is required, and `--no-cluster` to
        skip the LLM-driven community-labeling phase.

        The index is a runtime artefact; it is never checked in. Re-index here
        so later `graphify` queries (and the GraphifyBackend) hit the local
        store inside cloned repos.
        """
        for repo in repos:
            repo_path = cache_root / repo["name"]
            if not repo_path.exists():
                continue
            try:
                subprocess.run(
                    ["graphify", "extract", str(repo_path),
                     "--out", str(repo_path), "--code-only", "--no-cluster"],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=900,
                )
            except FileNotFoundError:
                # Graphify not installed; filesystem fallback still works.
                return
            except subprocess.CalledProcessError as e:
                log.warning("item repo=%s graphify index failed: %s", repo["name"], e)

    @staticmethod
    def _link_repo_cache(repos: list[dict], cache_root: Path, workspace: Path) -> None:
        workspace.mkdir(parents=True, exist_ok=True)
        for repo in repos:
            src = cache_root / repo["name"]
            dst = workspace / repo["name"]
            # ponytail: handle the broken-symlink case (cache evicted between
            # runs, but workspace symlink still pointing at the old target):
            # unlink and re-create. If dst is a real directory, leave it alone.
            if dst.is_symlink():
                dst.unlink()
            elif dst.exists():
                continue
            dst.symlink_to(src, target_is_directory=True)


def _now() -> float:
    """Wall-clock seconds. Imported lazily so tests can patch if needed."""
    import time as _t

    return _t.perf_counter()
