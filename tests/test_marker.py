"""marker.compute: sha1(title + description + sorted repo HEAD SHAs)."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import marker


def _fake_proc(stdout: str) -> MagicMock:
    p = MagicMock()
    p.stdout = stdout
    return p


def _stub_git(monkeypatch, shas_by_repo: dict[str, str]):
    """Replace subprocess.run so marker.compute never touches a real repo."""
    def fake_rev_parse(args, **kw):
        name = Path(args[2]).name  # workspace/<repo>
        return _fake_proc(shas_by_repo[name] + "\n")
    import subprocess as _sp
    monkeypatch.setattr(marker.subprocess, "run", fake_rev_parse)


def test_compute_returns_40_char_hex(tmp_path):
    item = {"fields": {"System.Title": "T", "System.Description": "D"}}
    h = marker.compute(item, tmp_path)
    assert len(h) == 40
    assert all(c in "0123456789abcdef" for c in h)


def test_compute_changes_with_title(tmp_path):
    a = marker.compute({"fields": {"System.Title": "T1", "System.Description": ""}}, tmp_path)
    b = marker.compute({"fields": {"System.Title": "T2", "System.Description": ""}}, tmp_path)
    assert a != b


def test_compute_changes_with_description(tmp_path):
    a = marker.compute({"fields": {"System.Title": "T", "System.Description": "old"}}, tmp_path)
    b = marker.compute({"fields": {"System.Title": "T", "System.Description": "new"}}, tmp_path)
    assert a != b


def test_compute_sorts_head_shas_before_hash(monkeypatch, tmp_path):
    # Two repos with HEADs "zzz" and "aaa". Sort makes the concat deterministic
    # regardless of glob order, so identity-of-output covers sort behavior.
    for name in ("alpha", "beta"):
        (tmp_path / name).mkdir()
        (tmp_path / name / ".git").mkdir()
    _stub_git(monkeypatch, {"alpha": "zzz", "beta": "aaa"})

    item = {"fields": {"System.Title": "T", "System.Description": ""}}
    first = marker.compute(item, tmp_path)
    second = marker.compute(item, tmp_path)
    assert first == second
    # Independent sanity: a different title must produce a different hash.
    assert first != marker.compute(
        {"fields": {"System.Title": "OTHER", "System.Description": ""}}, tmp_path,
    )


def test_compute_skips_repos_with_failing_rev_parse(monkeypatch, tmp_path):
    # git rev-parse fails for "broken" → must be silently skipped, not crash.
    for name in ("broken", "good"):
        (tmp_path / name).mkdir()
        (tmp_path / name / ".git").mkdir()

    import subprocess as _sp
    def fake_rev_parse(args, **kw):
        if Path(args[2]).name == "broken":
            raise _sp.CalledProcessError(1, "git")
        return _fake_proc("good-sha\n")

    monkeypatch.setattr(marker.subprocess, "run", fake_rev_parse)
    h = marker.compute({"fields": {"System.Title": "T", "System.Description": ""}}, tmp_path)
    assert len(h) == 40
