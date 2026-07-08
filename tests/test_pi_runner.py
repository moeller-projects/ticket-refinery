"""pi_runner.run: subprocess wrapper, happy + failure paths."""
import subprocess
from unittest.mock import patch

import pytest

import pi_runner


def _make_proc(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess("pi", returncode, stdout, stderr)


def test_run_returns_parsed_json(monkeypatch):
    payload = '{"facts": [], "dtos": [], "api_specs": [], "unknowns": [], "sourceRefs": []}'
    monkeypatch.setattr(
        pi_runner.subprocess, "run",
        lambda cmd, **kw: _make_proc(0, stdout=payload),
    )
    out = pi_runner.run("p", "model-x")
    assert out["facts"] == []


def test_run_passes_through_cli_flags(monkeypatch):
    captured = {}
    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return _make_proc(0, stdout='{"facts":[],"dtos":[],"api_specs":[],"unknowns":[],"sourceRefs":[]}')

    monkeypatch.setattr(pi_runner.subprocess, "run", fake_run)
    pi_runner.run("PROMPT", "m")

    assert captured["cmd"][0] == "pi"
    assert captured["cmd"][1:3] == ["-p", "PROMPT"]
    assert "--model" in captured["cmd"] and "m" in captured["cmd"]
    assert captured["kw"]["timeout"] == 600
    assert captured["kw"]["text"] is True
    assert captured["kw"]["capture_output"] is True


def test_run_missing_cli_raises_infraerror(monkeypatch):
    def fake_run(cmd, **kw):
        raise FileNotFoundError("pi: not found")

    monkeypatch.setattr(pi_runner.subprocess, "run", fake_run)
    with pytest.raises(pi_runner.InfraError, match="Pi CLI not on PATH"):
        pi_runner.run("p", "m")


def test_run_nonzero_exit_raises_infraerror(monkeypatch):
    monkeypatch.setattr(
        pi_runner.subprocess, "run",
        lambda cmd, **kw: _make_proc(2, stderr="boom"),
    )
    with pytest.raises(pi_runner.InfraError, match="exit 2"):
        pi_runner.run("p", "m")


def test_run_non_json_output_raises_infraerror(monkeypatch):
    monkeypatch.setattr(
        pi_runner.subprocess, "run",
        lambda cmd, **kw: _make_proc(0, stdout="not json at all"),
    )
    with pytest.raises(pi_runner.InfraError, match="not valid JSON"):
        pi_runner.run("p", "m")
