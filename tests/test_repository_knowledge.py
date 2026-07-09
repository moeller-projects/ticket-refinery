"""repository_knowledge: RepositoryKnowledge facade + backend selection."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import repository_knowledge as rk
from repository_knowledge import (
    ArchitectureSummary,
    DependencyEdge,
    DependencyGraph,
    DependencyNode,
    ExecutionPath,
    FilesystemBackend,
    GraphifyBackend,
    KnowledgeBackend,
    ReferenceHit,
    RelevantFiles,
    RepositoryKnowledge,
    SymbolHit,
    _MiniGraph,
    _parse_graph_json,
    make_knowledge,
)


# ---- backend detection / selection ----------------------------------------


def test_make_knowledge_uses_graphify_when_available(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n: f"/usr/local/bin/{n}" if n == "graphify" else None)
    knowledge = make_knowledge(project_path=tmp_path, force_backend=None)
    assert isinstance(knowledge, RepositoryKnowledge)
    assert knowledge.backend_name == "GraphifyBackend"


def test_make_knowledge_falls_back_to_filesystem_when_graphify_absent():
    with patch("shutil.which", return_value=None):
        knowledge = make_knowledge(project_path=Path("/tmp/anywhere"), force_backend=None)
    assert knowledge.backend_name == "FilesystemBackend"


def test_make_knowledge_force_backend_overrides_detection(tmp_path):
    knowledge = make_knowledge(project_path=tmp_path, force_backend="filesystem")
    assert knowledge.backend_name == "FilesystemBackend"


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
    fb = FilesystemBackend()
    assert fb.find_callees("Foo", project_path=Path("/tmp")) == []


def test_filesystem_impact_analysis_is_approximate(tmp_path):
    (tmp_path / "a.py").write_text("alpha\n")
    fb = FilesystemBackend()
    out = fb.impact_analysis("alpha", project_path=tmp_path)
    assert out["approximate"] is True
    assert "FilesystemBackend" in out["note"]


def test_filesystem_curated_ops_are_degraded(tmp_path):
    fb = FilesystemBackend()
    arch = fb.architecture_summary(project_path=tmp_path)
    assert arch.degraded is True
    deps = fb.dependency_graph(project_path=tmp_path)
    assert deps.degraded is True
    ep = fb.execution_path("foo", project_path=tmp_path)
    assert ep.degraded is True
    rf = fb.relevant_files("alpha", project_path=tmp_path)
    assert rf.degraded is True


def test_filesystem_relevant_files_falls_back_to_grep(tmp_path):
    (tmp_path / "a.py").write_text("alpha here\n")
    fb = FilesystemBackend()
    rf = fb.relevant_files("alpha", project_path=tmp_path)
    assert rf.degraded is True  # still degraded marker; caller knows
    assert any("a.py" in f for f in rf.files)


# ---- GraphifyBackend: fixtures + behaviour ---------------------------------


def _write_graph(project_path: Path, *, nodes: dict, edges: list[tuple[str, str, dict]]) -> Path:
    """Helper: drop a node_link_graph JSON into `<project>/graphify-out/graph.json`."""
    graph_dir = project_path / "graphify-out"
    graph_dir.mkdir(parents=True, exist_ok=True)
    path = graph_dir / "graph.json"
    payload = {
        "directed": True,
        "multigraph": False,
        "nodes": nodes,
        "links": [
            {"source": u, "target": v, **attrs} for u, v, attrs in edges
        ],
    }
    path.write_text(json.dumps(payload))
    return path


def test_graphify_status_reports_graph_file(tmp_path):
    _write_graph(tmp_path, nodes={"a": {"label": "A"}}, edges=[])
    gb = GraphifyBackend(cli="/bin/true")
    out = gb.status(tmp_path)
    assert out["backend"] == "graphify"
    assert out["ok"] is True
    assert out["graph"].endswith("graph.json")


def test_graphify_status_triggers_build_when_missing(tmp_path, monkeypatch):
    """If graph.json is absent, status() invokes `graphify extract` to build it."""
    fake = tmp_path / "graphify"
    out_dir = tmp_path

    def fake_cli(*args, **kwargs):
        graph_dir = out_dir / "graphify-out"
        graph_dir.mkdir(exist_ok=True)
        (graph_dir / "graph.json").write_text(json.dumps({"nodes": {}, "links": []}))
        return _run_ok()
    monkeypatch.setattr(rk.subprocess, "run", fake_cli)
    gb = GraphifyBackend(cli=str(fake))
    out = gb.status(tmp_path)
    assert out["ok"] is True


def test_graphify_missing_cli_raises_file_not_found():
    with patch("shutil.which", return_value=None):
        with pytest.raises(FileNotFoundError):
            GraphifyBackend()


def test_graphify_find_symbol_uses_graph_json(tmp_path):
    """Symbol lookup reads graph.json, not a CLI call per query."""
    nodes = {
        "n1": {"label": "OrderService", "kind": "class", "source_file": "orders.py", "start_line": 10},
        "n2": {"label": "OtherService", "kind": "class", "source_file": "other.py", "start_line": 5},
    }
    _write_graph(tmp_path, nodes=nodes, edges=[])
    gb = GraphifyBackend(cli="/bin/true")
    out = gb.find_symbol("Order", project_path=tmp_path)
    names = [h.name for h in out]
    assert "OrderService" in names
    assert all(h.kind == "class" for h in out)


def test_graphify_find_symbol_kind_filter(tmp_path):
    nodes = {
        "n1": {"label": "do_thing", "kind": "function", "source_file": "a.py", "start_line": 1},
        "n2": {"label": "do_thing", "kind": "class", "source_file": "a.py", "start_line": 9},
    }
    _write_graph(tmp_path, nodes=nodes, edges=[])
    gb = GraphifyBackend(cli="/bin/true")
    out = gb.find_symbol("do_thing", project_path=tmp_path, kind="class")
    assert len(out) == 1
    assert out[0].kind == "class"


def test_graphify_find_callers_and_callees(tmp_path):
    nodes = {
        "a": {"label": "Alpha", "kind": "function", "source_file": "a.py", "start_line": 1},
        "b": {"label": "Beta",  "kind": "function", "source_file": "b.py", "start_line": 1},
        "c": {"label": "Gamma", "kind": "function", "source_file": "c.py", "start_line": 1},
    }
    edges = [
        ("a", "b", {"relation": "calls", "source_line": 5}),
        ("c", "b", {"relation": "calls", "source_line": 12}),
    ]
    _write_graph(tmp_path, nodes=nodes, edges=edges)
    gb = GraphifyBackend(cli="/bin/true")
    callers = gb.find_callers("Beta", project_path=tmp_path)
    callees = gb.find_callees("Beta", project_path=tmp_path)
    assert {c.symbol for c in callers} >= {"Alpha", "Gamma"}  # inbound labels
    assert callees == []  # Beta calls no one
    # Backend re-roots source_file to absolute paths (so downstream file
    # validation can resolve them); each `file` is now rooted under tmp_path.
    assert all(Path(c.file).is_absolute() for c in callers)
    assert all(Path(c.file).is_relative_to(tmp_path) for c in callers)


def test_graphify_impact_analysis_walks_inbound_edges(tmp_path):
    nodes = {
        "a": {"label": "A", "kind": "function", "source_file": "a.py", "start_line": 1},
        "b": {"label": "B", "kind": "function", "source_file": "b.py", "start_line": 1},
        "c": {"label": "C", "kind": "function", "source_file": "c.py", "start_line": 1},
    }
    edges = [
        ("a", "b", {"relation": "calls"}),
        ("c", "a", {"relation": "calls"}),
    ]
    _write_graph(tmp_path, nodes=nodes, edges=edges)
    gb = GraphifyBackend(cli="/bin/true")
    out = gb.impact_analysis("B", project_path=tmp_path, depth=3)
    assert out["resolved"] == "B"
    assert len(out["callers"]) == 2  # a (depth 1) and c (depth 2)


def test_graphify_architecture_summary_groups_by_top_dir(tmp_path):
    """Modules group by the immediate parent directory of each source file.
    .sln/.csproj/.ps1/etc. are filtered out — they belong to project config,
    not code, and they bloated the curated prompt."""
    nodes = {
        # Real source files — each module needs ≥2 files to surface.
        "no1": {"label": "n1", "kind": "function", "source_file": "src/orders/x.py", "start_line": 1},
        "no2": {"label": "n2", "kind": "function", "source_file": "src/orders/y.py", "start_line": 1},
        "no3": {"label": "n3", "kind": "function", "source_file": "src/payments/a.py", "start_line": 1},
        "no4": {"label": "n4", "kind": "function", "source_file": "src/payments/b.py", "start_line": 1},
        "no5": {"label": "n5", "kind": "function", "source_file": "src/shared/c.py", "start_line": 1},
        "no6": {"label": "n6", "kind": "function", "source_file": "src/shared/d.py", "start_line": 1},
        # Project config — must be filtered.
        "np1": {"label": "p1", "kind": "config", "source_file": "Laekkerai.Ordering.sln"},
        "np2": {"label": "p2", "kind": "script", "source_file": "Install-Requirements.ps1"},
        "np3": {"label": "p3", "kind": "config", "source_file": "modules/x/y.csproj"},
    }
    _write_graph(tmp_path, nodes=nodes, edges=[])
    gb = GraphifyBackend(cli="/bin/true")
    out = gb.architecture_summary(project_path=tmp_path)
    assert out.degraded is False
    assert "orders" in out.modules
    assert "payments" in out.modules
    assert "shared" in out.modules
    # Filtered artefacts don't appear in the rendered text.
    assert "Laekkerai.Ordering.sln" not in out.text
    assert "Install-Requirements.ps1" not in out.text
    assert "6 source files" in out.text


def test_graphify_dependency_graph_collects_file_edges(tmp_path):
    nodes = {
        "a1": {"label": "a1", "kind": "function", "source_file": "a.py", "start_line": 1},
        "a2": {"label": "a2", "kind": "function", "source_file": "a.py", "start_line": 5},
        "b1": {"label": "b1", "kind": "function", "source_file": "b.py", "start_line": 1},
    }
    edges = [
        ("a1", "b1", {"relation": "calls"}),
        ("a2", "b1", {"relation": "calls"}),
    ]
    _write_graph(tmp_path, nodes=nodes, edges=edges)
    gb = GraphifyBackend(cli="/bin/true")
    out = gb.dependency_graph(project_path=tmp_path)
    assert out.degraded is False
    # Source files are re-rooted to absolute paths under tmp_path during merge.
    names = {Path(n.path).name for n in out.nodes}
    assert {"a.py", "b.py"} <= names
    relations = {(Path(e.source).name, Path(e.target).name, e.kind) for e in out.edges}
    assert ("a.py", "b.py", "calls") in relations


def test_graphify_execution_path_lists_direct_callees(tmp_path):
    nodes = {
        "a": {"label": "A", "kind": "function", "source_file": "a.py", "start_line": 1},
        "b": {"label": "B", "kind": "function", "source_file": "b.py", "start_line": 1},
    }
    _write_graph(tmp_path, nodes=nodes, edges=[("a", "b", {"relation": "calls"})])
    gb = GraphifyBackend(cli="/bin/true")
    out = gb.execution_path("A", project_path=tmp_path)
    assert out.degraded is False
    assert len(out.path) == 1
    # File path is re-rooted to absolute under tmp_path during merge.
    assert Path(out.path[0].file).name == "b.py"


def test_graphify_relevant_files_scores_by_label(tmp_path):
    nodes = {
        "n1": {"label": "OrderService", "kind": "class", "source_file": "orders.py", "start_line": 1},
        "n2": {"label": "PaymentService", "kind": "class", "source_file": "payments.py", "start_line": 1},
        "n3": {"label": "Unrelated", "kind": "class", "source_file": "misc.py", "start_line": 1},
    }
    _write_graph(tmp_path, nodes=nodes, edges=[])
    gb = GraphifyBackend(cli="/bin/true")
    out = gb.relevant_files("Order", project_path=tmp_path)
    assert out.degraded is False
    # Files are re-rooted to absolute paths under tmp_path during merge.
    names = {Path(f).name for f in out.files}
    assert "orders.py" in names
    assert "misc.py" not in names


def test_graphify_discovers_graphs_in_workspace_subdirs(tmp_path):
    """When project_path is a workspace with multiple cloned-repo subdirs
    (the runtime layout from WorkspaceService._sync_graphify_indexes),
    the backend finds each repo's graph.json and merges them into one
    combined graph. This is the symptom reported in the production run:
    the workspace symlink layout made extract produce an empty graph,
    but the actual graph.json at `<repo>/graphify-out/graph.json` is fine.
    """
    repo_a = tmp_path / "RepoA"
    repo_b = tmp_path / "RepoB"
    nodes_a = {"a1": {"label": "OrderService", "kind": "class",
                       "source_file": "a.py", "start_line": 1}}
    nodes_b = {"b1": {"label": "PaymentService", "kind": "class",
                       "source_file": "b.py", "start_line": 1}}
    _write_graph(repo_a, nodes=nodes_a, edges=[])
    _write_graph(repo_b, nodes=nodes_b, edges=[])
    gb = GraphifyBackend(cli="/bin/true")
    out = gb.find_symbol("Order", project_path=tmp_path)
    names = [h.name for h in out]
    assert "OrderService" in names
    files = [h.file for h in out]
    assert all(Path(f).is_absolute() for f in files)
    # Namespaced so the graph carries the repo origin in node IDs.
    assert any("RepoA" in nid for nid in gb._load_graph(tmp_path)["nodes"])


def test_graphify_workspace_with_no_graphs_returns_none(tmp_path):
    """Symptom of the production failure: extract ran on the workspace
    dir (where symlinks point into the cache_root), graphify doesn't
    follow symlinks, graph.json is missing everywhere. We surface this as
    "no graph available" rather than fabricating an empty graph."""
    (tmp_path / "SymlinkedRepo").symlink_to("/nonexistent/path")
    gb = GraphifyBackend(cli="/bin/true")
    assert gb._load_graph(tmp_path) is None
    assert gb.find_symbol("anything", project_path=tmp_path) == []
    assert gb.architecture_summary(project_path=tmp_path).degraded is True


def test_graphify_relevant_files_returns_degraded_for_empty_query(tmp_path):
    _write_graph(tmp_path, nodes={"n1": {"label": "A", "source_file": "a.py"}}, edges=[])
    gb = GraphifyBackend(cli="/bin/true")
    out = gb.relevant_files("", project_path=tmp_path)
    assert out.degraded is True


# ---- MiniGraph parser ------------------------------------------------------


def test_minigraph_parses_node_link_json():
    raw = {
        "directed": True, "multigraph": False,
        "nodes": {"n1": {"label": "A"}, "n2": {"label": "B"}},
        "links": [{"source": "n1", "target": "n2", "relation": "calls"}],
    }
    g, nodes = _parse_graph_json(raw)
    assert "n1" in g.nodes and "n2" in g.nodes
    out = g.out_edges("n1")
    assert len(out) == 1 and out[0][1] == "n2"
    assert "B" in [nodes[n]["label"] for n in nodes]


def test_minigraph_accepts_edges_key():
    raw = {
        "nodes": {"n1": {"label": "A"}, "n2": {"label": "B"}},
        "edges": [{"source": "n1", "target": "n2"}],
    }
    g, _ = _parse_graph_json(raw)
    assert len(g.out_edges("n1")) == 1


def test_minigraph_handles_list_nodes():
    raw = {
        "nodes": [{"id": "n1"}, {"id": "n2"}],
        "links": [{"source": "n1", "target": "n2"}],
    }
    g, _ = _parse_graph_json(raw)
    assert g.successors("n1") == {"n2"}


# ---- RepositoryKnowledge facade -------------------------------------------


def test_repository_knowledge_delegates_to_backend(tmp_path):
    fb = FilesystemBackend()
    knowledge = RepositoryKnowledge(fb, project_path=tmp_path)
    assert knowledge.project_path == tmp_path
    assert knowledge.backend_name == "FilesystemBackend"
    status = knowledge.status()
    assert status["backend"] == "filesystem"


def test_repository_knowledge_passes_project_through(tmp_path):
    (tmp_path / "a.py").write_text("needle here\n")
    fb = FilesystemBackend()
    knowledge = RepositoryKnowledge(fb, project_path=tmp_path)
    out = knowledge.search("needle")
    assert out and "a.py:1:needle here" in out[0]


def test_repository_knowledge_curated_ops_pass_through(tmp_path):
    captured: dict = {}

    class SpyBackend(KnowledgeBackend):
        def status(self, project_path): return {"backend": "spy", "ok": True}
        def search_text(self, q, *, project_path, top_k=50): return []
        def find_symbol(self, name, *, project_path, kind=None): return []
        def find_callers(self, symbol, *, project_path): return []
        def find_callees(self, symbol, *, project_path): return []
        def find_references(self, name, *, project_path): return []
        def find_implementations(self, name, *, project_path): return []
        def impact_analysis(self, symbol, *, project_path, depth=2): return {}
        def architecture_summary(self, *, project_path):
            captured["architecture"] = project_path
            return ArchitectureSummary(text="x", modules=("m",))
        def dependency_graph(self, *, project_path):
            captured["deps"] = project_path
            return DependencyGraph()
        def execution_path(self, symbol, *, project_path):
            captured["exec"] = (symbol, project_path)
            return ExecutionPath(symbol=symbol)
        def relevant_files(self, query, *, project_path, top_k=20):
            captured["relevant"] = (query, project_path)
            return RelevantFiles(query=query, files=("a.py",))

    k = RepositoryKnowledge(SpyBackend(), project_path=tmp_path)
    k.architecture_summary()
    k.dependency_graph()
    k.execution_path("foo")
    k.relevant_files("needle")
    assert captured["architecture"] == tmp_path
    assert captured["deps"] == tmp_path
    assert captured["exec"] == ("foo", tmp_path)
    assert captured["relevant"] == ("needle", tmp_path)


# ---- DTO conversions -----------------------------------------------------


def test_dependency_graph_to_dict_roundtrip():
    g = DependencyGraph(
        nodes=(DependencyNode(path="a.py", language="python"),),
        edges=(DependencyEdge(source="a.py", target="b.py"),),
    )
    d = g.to_dict()
    assert d["nodes"][0]["path"] == "a.py"
    assert d["edges"][0]["source"] == "a.py"
    assert d["edges"][0]["kind"] == "references"


def test_execution_path_to_dict():
    ep = ExecutionPath(symbol="foo", path=(ReferenceHit(file="a.py", line=3, symbol="foo"),))
    d = ep.to_dict()
    assert d["symbol"] == "foo"
    assert d["path"][0]["file"] == "a.py"


def test_relevant_files_to_dict():
    rf = RelevantFiles(query="needle", files=("a.py", "b.py"))
    d = rf.to_dict()
    assert d["query"] == "needle"
    assert d["files"] == ["a.py", "b.py"]


# ---- legacy aliases for migration ----------------------------------------


def test_legacy_aliases_still_resolve():
    """Older code paths still work during the transition."""
    from repository_knowledge import ExplorerBackend, RepositoryExplorer, CodeGraphBackend  # type: ignore
    assert RepositoryExplorer is RepositoryKnowledge
    assert ExplorerBackend is KnowledgeBackend
    assert CodeGraphBackend is GraphifyBackend


def _run_ok():
    from unittest.mock import MagicMock
    return MagicMock(returncode=0, stdout="", stderr="")