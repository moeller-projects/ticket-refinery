"""WorkspaceService: clone, link, cleanup. git_ops is monkeypatched per test."""
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import git_ops
from services.workspace_service import Workspace, WorkspaceService


def _repos(names=("alpha",)):
    return [{"name": n, "url": f"https://example/{n}.git", "defaultBranch": "main"} for n in names]


def _stub_clones(monkeypatch):
    """Make clone_all a no-op; tests that need a fake cache set it up themselves."""
    monkeypatch.setattr(git_ops, "clone_all", lambda repos, root, depth, pat: None)


def test_workspace_dataclass_lists_repos():
    ws = Workspace(path=Path("/w"), repo_names=("alpha", "beta"))
    assert [p.name for p in ws.repos] == ["alpha", "beta"]


def test_prepare_clones_into_workspace_when_no_cache_root(monkeypatch):
    _stub_clones(monkeypatch)
    svc = WorkspaceService()
    ws = svc.prepare(item_id=7, repos=_repos(), depth=1, pat=None)
    assert ws.path == Path("/tmp/refine-7")
    assert ws.repo_names == ("alpha",)


def test_prepare_links_when_cache_root_separate(tmp_path, monkeypatch):
    _stub_clones(monkeypatch)
    cache = tmp_path / "cache"
    (cache / "alpha").mkdir(parents=True)
    (cache / "alpha" / ".git").mkdir()
    svc = WorkspaceService(cache_root=cache)
    ws = svc.prepare(item_id=8, repos=_repos(), depth=1, pat=None)
    assert ws.path == Path("/tmp/refine-8")
    link = ws.path / "alpha"
    assert link.is_symlink(), "alpha should be a symlink to cache"
    assert link.resolve() == (cache / "alpha").resolve()


def test_link_repo_cache_skips_existing_dest_symlink(tmp_path):
    """Broken previous symlinks (target deleted between runs) should not cause FileExistsError."""
    cache = tmp_path / "c"
    (cache / "alpha").mkdir(parents=True)
    ws = tmp_path / "w"
    ws.mkdir()
    # Pre-existing symlink pointing to nowhere.
    (ws / "alpha").symlink_to(tmp_path / "nope", target_is_directory=True)
    WorkspaceService._link_repo_cache(_repos(), cache, ws)
    # Replaced with the right target.
    assert (ws / "alpha").is_symlink()
    assert (ws / "alpha").resolve() == (cache / "alpha").resolve()


def test_link_repo_cache_skips_when_dst_already_a_real_directory(tmp_path):
    cache = tmp_path / "c"
    (cache / "alpha").mkdir(parents=True)
    ws = tmp_path / "w"
    ws.mkdir()
    (ws / "alpha").mkdir()
    WorkspaceService._link_repo_cache(_repos(), cache, ws)
    # Untouched — not a symlink.
    assert not (ws / "alpha").is_symlink()


def test_cleanup_runs_git_ops_cleanup(monkeypatch):
    seen = []
    monkeypatch.setattr(git_ops, "cleanup", lambda p: seen.append(p))
    svc = WorkspaceService()
    svc.cleanup(Path("/tmp/refine-7"))
    assert seen == [Path("/tmp/refine-7")]


def test_prepare_records_clone_duration(monkeypatch):
    _stub_clones(monkeypatch)
    times = []
    svc = WorkspaceService()
    svc.prepare(
        item_id=7,
        repos=_repos(),
        depth=1,
        pat=None,
        on_clone_duration=lambda s: times.append(s),
    )
    assert len(times) == 1
    assert times[0] >= 0


def test_prepare_retries_transient_clone_failure(monkeypatch):
    attempts = []

    def _flaky(repos, root, depth, pat):
        attempts.append(1)
        if len(attempts) < 3:
            raise subprocess.CalledProcessError(128, ["git"])

    monkeypatch.setattr(git_ops, "clone_all", _flaky)
    svc = WorkspaceService()
    ws = svc.prepare(item_id=11, repos=_repos(), depth=1, pat=None)
    assert len(attempts) == 3  # 2 fails + 1 success
    assert ws.path == Path("/tmp/refine-11")


def test_prepare_propagates_git_failure_after_all_attempts(monkeypatch):
    monkeypatch.setattr(
        git_ops, "clone_all",
        lambda *a, **kw: (_ for _ in ()).throw(subprocess.CalledProcessError(128, ["git"])),
    )
    svc = WorkspaceService()
    with pytest.raises(subprocess.CalledProcessError):
        svc.prepare(item_id=12, repos=_repos(), depth=1, pat=None)


def test_shutdown_cleans_cache(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "alpha").mkdir()
    svc = WorkspaceService(cache_root=cache)
    svc.shutdown()
    assert not cache.exists()


def test_shutdown_is_noop_when_no_cache_root():
    svc = WorkspaceService()
    svc.shutdown()  # must not raise even though no cache was configured


# ---- graphify sync --------------------------------------------------------


def test_prepare_invokes_graphify_install_per_repo(monkeypatch, tmp_path):
    """Each cloned repo gets `graphify extract` run inside it so the AST
    index is fresh before the orchestrator queries GraphifyBackend."""
    _stub_clones(monkeypatch)
    cache = tmp_path / "cache"
    (cache / "alpha").mkdir(parents=True)
    (cache / "alpha" / ".git").mkdir()
    monkeypatch.setattr(git_ops, "clone_all", lambda repos, root, depth, pat: None)
    runs: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        runs.append(cmd)
        if cmd[:1] == ["graphify"] and "extract" in cmd:
            cwd = kwargs.get("cwd")
            (Path(cwd) / "graphify-out").mkdir(exist_ok=True)
        from unittest.mock import MagicMock
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("services.workspace_service.subprocess.run", fake_run)
    svc = WorkspaceService(cache_root=cache)
    svc.prepare(item_id=42, repos=_repos(), depth=1, pat=None)
    extracts = [c for c in runs if c[:2] == ["graphify", "extract"]]
    assert extracts, "graphify extract should have been invoked at least once"
    # Verify it passes the relevant flags: code-only (no LLM) + --out dir.
    extract_args = extracts[0]
    assert "--code-only" in extract_args
    assert "--no-cluster" in extract_args


def test_prepare_skips_graphify_when_cli_missing(monkeypatch, tmp_path):
    """If the graphify CLI isn't installed, sync is a no-op (FilesystemBackend
    still works downstream)."""
    _stub_clones(monkeypatch)
    cache = tmp_path / "cache"
    (cache / "alpha").mkdir(parents=True)
    (cache / "alpha" / ".git").mkdir()
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        from unittest.mock import MagicMock
        if cmd[:2] == ["graphify", "extract"]:
            raise FileNotFoundError("graphify not found")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("services.workspace_service.subprocess.run", fake_run)
    svc = WorkspaceService(cache_root=cache)
    ws = svc.prepare(item_id=43, repos=_repos(), depth=1, pat=None)
    assert ws.path == Path("/tmp/refine-43")
    extract_calls = [c for c in calls if c[:2] == ["graphify", "extract"]]
    assert extract_calls, f"expected graphify extract attempts, got {calls}"


def test_prepare_swallows_graphify_install_errors(monkeypatch, tmp_path):
    """A failing `graphify extract` is logged and skipped — the workspace
    is still returned. The orchestrator will then run with a degraded
    knowledge backend."""
    _stub_clones(monkeypatch)
    cache = tmp_path / "cache"
    (cache / "alpha").mkdir(parents=True)
    (cache / "alpha" / ".git").mkdir()

    def fake_run(cmd, **kwargs):
        from unittest.mock import MagicMock
        if cmd[:2] == ["graphify", "extract"]:
            raise subprocess.CalledProcessError(1, cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("services.workspace_service.subprocess.run", fake_run)
    svc = WorkspaceService(cache_root=cache)
    ws = svc.prepare(item_id=44, repos=_repos(), depth=1, pat=None)
    assert ws.path == Path("/tmp/refine-44")


def test_prepare_no_longer_uses_codegraph_cli(monkeypatch, tmp_path):
    """Regression guard: the old `codegraph init` call must NOT appear
    anywhere in the workspace preparation flow."""
    _stub_clones(monkeypatch)
    cache = tmp_path / "cache"
    (cache / "alpha").mkdir(parents=True)
    (cache / "alpha" / ".git").mkdir()
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        from unittest.mock import MagicMock
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("services.workspace_service.subprocess.run", fake_run)
    svc = WorkspaceService(cache_root=cache)
    svc.prepare(item_id=45, repos=_repos(), depth=1, pat=None)
    assert not any(c and c[0] == "codegraph" for c in calls), \
        f"stale codegraph CLI invocation: {calls}"
