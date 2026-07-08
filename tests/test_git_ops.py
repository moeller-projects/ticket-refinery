"""git_ops: subprocess.run is mocked; clone_all parallelizes via map()."""
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import git_ops


def test_clone_one_invokes_git_with_extra_header_and_pat(monkeypatch, tmp_path):
    target = tmp_path / "r1"
    repo = {"name": "r1", "url": "https://example.com/x.git", "defaultBranch": "dev"}
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env", {})
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)
    git_ops._clone_one(repo, tmp_path, depth=1, pat="secret-pat")

    assert captured["cmd"][:2] == ["git", "clone"]
    assert "--depth" in captured["cmd"] and "1" in captured["cmd"]
    assert "--branch" in captured["cmd"] and "dev" in captured["cmd"]
    assert captured["cmd"][-2:] == ["https://example.com/x.git", str(target)]
    # PAT stays in the env, not on the command line.
    assert "secret-pat" not in " ".join(captured["cmd"])
    assert captured["env"]["GIT_CONFIG_COUNT"] == "1"
    assert captured["env"]["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert captured["env"]["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "secret-pat" not in captured["env"]["GIT_CONFIG_VALUE_0"]  # base64'd, raw value absent
    # Real PAT must NOT leak into env either (header is base64 of ':pat', not literal).
    assert "secret-pat" not in str(captured["env"])


def test_clone_one_skips_when_target_already_a_git_repo(tmp_path):
    target = tmp_path / "r2"
    (target / ".git").mkdir(parents=True)
    with patch.object(git_ops.subprocess, "run") as run:
        git_ops._clone_one(
            {"name": "r2", "url": "x", "defaultBranch": "main"},
            tmp_path, depth=1, pat="p",
        )
    run.assert_not_called()


def test_clone_one_no_pat_keeps_clean_env(monkeypatch, tmp_path):
    repo = {"name": "r", "url": "https://example.com/x.git", "defaultBranch": "main"}
    captured = {}
    monkeypatch.setattr(git_ops.subprocess, "run",
                        lambda cmd, **kw: (captured.update(env=kw.get("env"))) or subprocess.CompletedProcess(cmd, 0, "", ""))
    git_ops._clone_one(repo, tmp_path, depth=1, pat=None)
    # No PAT → no extraHeader env keys set (or at minimum the count remains absent).
    assert "GIT_CONFIG_COUNT" not in captured["env"]


def test_clone_all_empty_short_circuits(tmp_path):
    git_ops.clone_all([], tmp_path, depth=1, pat="p")  # must not raise, no threads spawned
    assert tmp_path.exists()


def test_clone_all_runs_each_repo(monkeypatch, tmp_path):
    runs: list[str] = []
    monkeypatch.setattr(git_ops.subprocess, "run",
                        lambda cmd, **kw: runs.append(cmd[-1]) or subprocess.CompletedProcess(cmd, 0, "", ""))
    repos = [
        {"name": "a", "url": "u-a", "defaultBranch": "main"},
        {"name": "b", "url": "u-b", "defaultBranch": "main"},
    ]
    git_ops.clone_all(repos, tmp_path, depth=1, pat=None)
    assert set(runs) == {str(tmp_path / "a"), str(tmp_path / "b")}


def test_cleanup_removes_workspace(tmp_path):
    work = tmp_path / "x"
    work.mkdir()
    (work / "f").write_text("x")
    git_ops.cleanup(work)
    assert not work.exists()


def test_cleanup_swallows_missing(tmp_path):
    # Missing dir → no exception.
    git_ops.cleanup(tmp_path / "never-existed")
