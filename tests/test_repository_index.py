"""repository_index: RepositoryExplorer facade + backend selection."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import repository_index as ri
from repository_index import (
    CodeGraphBackend,
    ExplorerBackend,
    FilesystemBackend,
    ReferenceHit,
    RepositoryExplorer,
    SymbolHit,
    make_explorer,
)


# ---- backend detection / selection ----------------------------------------


def test_make_explorer_uses_codegraph_when_available(tmp_path):
    fake_codegraph = "/tmp/fake-codegraph"
    Path(fake_codegraph).write_text("#!/bin/sh\nexit 0\n")
    Path(fake_codegraph).chmod(0o755)
    explorer = make_explorer(project_path=tmp_path, force_backend="codegraph", cli=fake_codegraph)
    assert isinstance(explorer, RepositoryExplorer)
    assert explorer.backend_name == "CodeGraphBackend"


def test_make_explorer_falls_back_to_filesystem_when_codegraph_absent():
    with patch("shutil.which", return_value=None):
        explorer = make_explorer(project_path=Path("/tmp/anywhere"), force_backend=None)
    assert explorer.backend_name == "FilesystemBackend"


def test_make_explorer_force_backend_overrides_detection(tmp_path):
    explorer = make_explorer(project_path=tmp_path, force_backend="filesystem")
    assert explorer.backend_name == "FilesystemBackend"


# ---- FilesystemBackend ----------------------------------------------------


def test_filesystem_backend_status_reports_existence(tmp_path):
    fb = FilesystemBackend()
    assert fb.status(tmp_path)["ok"] is True
    assert fb.status(tmp_path / "missing")["ok"] is False


def test_filesystem_search_text_returns_lines(tmp_path):
    (tmp_path / "a.py").write_text("alpha\nbeta\nalpha\n")
    fb = FilesystemBackend()
    out = fb.search_text("alpha", project_path=tmp_path)
    assert len(out) == 2
    # grep, run with cwd=tmp_path, emits `./`-prefixed paths.
    assert any(line.endswith("a.py:1:alpha") for line in out)
    assert any(line.endswith("a.py:3:alpha") for line in out)


def test_filesystem_find_symbol_returns_hits_in_correct_project(tmp_path):
    (tmp_path / "a.py").write_text("alpha symbol on line 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("alpha inside sub\n")
    fb = FilesystemBackend()
    hits = fb.find_symbol("alpha", project_path=tmp_path)
    paths = [h.file for h in hits]
    assert any(p.endswith("a.py") for p in paths)
    assert all(Path(p).is_relative_to(tmp_path) for p in paths)


def test_filesystem_find_implementations_returns_empty():
    fb = FilesystemBackend()
    assert fb.find_implementations("Foo", project_path=Path("/tmp")) == []


def test_filesystem_find_callees_returns_empty():
    """Filesystem cannot resolve call graph."""
    fb = FilesystemBackend()
    assert fb.find_callees("Foo", project_path=Path("/tmp")) == []


def test_filesystem_impact_analysis_is_approximate(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("alpha\n")
    fb = FilesystemBackend()
    out = fb.impact_analysis("alpha", project_path=tmp_path)
    assert out["approximate"] is True
    assert "FilesystemBackend" in out["note"]


# ---- CodeGraphBackend (using fake CLI) ------------------------------------


def _fake_codegraph_script(directory: Path) -> Path:
    """A short shell script that emits JSON the backend can parse."""
    script = directory / "cg"
    script.write_text(
        "#!/bin/sh\n"
        "echo '[{\"node\": {\"qualifiedName\": \"foo\", \"kind\": \"function\", "
        "\"filePath\": \"/tmp/foo.py\", \"startLine\": 12, \"signature\": \"() -> int\"}}]'\n"
    )
    script.chmod(0o755)
    return script


def test_codegraph_backend_status_ok_with_real_cli(tmp_path):
    cb = CodeGraphBackend(cli=_fake_codegraph_script(tmp_path))
    out = cb.status(tmp_path)
    assert out["backend"] == "codegraph"
    assert out["ok"] is True


def test_codegraph_find_symbol_parses_json(tmp_path):
    cb = CodeGraphBackend(cli=_fake_codegraph_script(tmp_path))
    hits = cb.find_symbol("foo", project_path=tmp_path)
    assert len(hits) == 1
    h = hits[0]
    assert h.name == "foo"
    assert h.kind == "function"
    assert h.file == "/tmp/foo.py"
    assert h.line == 12


def test_codegraph_missing_cli_raises_file_not_found():
    with patch("shutil.which", return_value=None):
        with pytest.raises(FileNotFoundError):
            CodeGraphBackend()


def test_codegraph_run_handles_non_json_preamble(tmp_path, monkeypatch):
    """`codegraph` occasionally prints progress lines before the JSON payload.
    The backend should still decode the JSON when it scans for the first {."""
    script = tmp_path / "cg"
    script.write_text("#!/bin/sh\necho 'starting...'\necho '[{\"node\":{}}]'\n")
    script.chmod(0o755)
    cb = CodeGraphBackend(cli=str(script))
    out = cb._run(["query", "x"], project_path=tmp_path)
    assert isinstance(out, list)


def test_codegraph_callers_parses_caller_payload(tmp_path):
    script = tmp_path / "cg"
    script.write_text(
        '#!/bin/sh\n'
        'echo \'[{"caller":{"qualifiedName":"bar","filePath":"/x.py","startLine":7}}]\'\n'
    )
    script.chmod(0o755)
    cb = CodeGraphBackend(cli=str(script))
    out = cb.find_callers("foo", project_path=tmp_path)
    assert len(out) == 1
    assert out[0].file == "/x.py"
    assert out[0].line == 7


# ---- RepositoryExplorer facade -------------------------------------------


def test_repository_explorer_delegates_to_backend(tmp_path):
    fb = FilesystemBackend()
    explorer = RepositoryExplorer(fb, project_path=tmp_path)
    assert explorer.project_path == tmp_path
    assert explorer.backend_name == "FilesystemBackend"
    status = explorer.status()
    assert status["backend"] == "filesystem"


def test_repository_explorer_passes_project_through(tmp_path):
    (tmp_path / "a.py").write_text("needle here\n")
    fb = FilesystemBackend()
    explorer = RepositoryExplorer(fb, project_path=tmp_path)
    out = explorer.search_text("needle")
    assert out and "a.py:1:needle here" in out[0]
