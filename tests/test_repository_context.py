"""repository_context: RepositoryContextBuilder + RepositoryContext rendering."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import repository_context as rc
from repository_context import (
    RepositoryContext,
    RepositoryContextBuilder,
)
from repository_knowledge import (
    ArchitectureSummary,
    DependencyEdge,
    DependencyGraph,
    DependencyNode,
    KnowledgeBackend,
    RelevantFiles,
    RepositoryKnowledge,
)


def _stub_backend(**overrides) -> KnowledgeBackend:
    """Return a backend stub with curated op defaults and per-test overrides."""

    class Stub(KnowledgeBackend):
        def __init__(self, **kw):
            self.kw = kw

        def status(self, project_path): return {"backend": "stub", "ok": True}
        def search_text(self, query, *, project_path, top_k=50): return []
        def find_symbol(self, name, *, project_path, kind=None): return []
        def find_callers(self, symbol, *, project_path): return []
        def find_callees(self, symbol, *, project_path): return []
        def find_references(self, name, *, project_path): return []
        def find_implementations(self, name, *, project_path): return []
        def impact_analysis(self, symbol, *, project_path, depth=2):
            return {"symbol": symbol, "callers": [], "approximate": False}

        def architecture_summary(self, *, project_path):
            return ArchitectureSummary(
                text="summary text",
                modules=self.kw.get("modules", ("orders", "payments")),
                degraded=self.kw.get("arch_degraded", False),
            )

        def dependency_graph(self, *, project_path):
            if self.kw.get("deps_degraded"):
                return DependencyGraph(degraded=True)
            return DependencyGraph(
                nodes=(DependencyNode(path="orders.py"), DependencyNode(path="payments.py")),
                edges=(DependencyEdge(source="orders.py", target="payments.py"),),
            )

        def relevant_files(self, query, *, project_path, top_k=20):
            return RelevantFiles(
                query=query,
                files=self.kw.get("relevant", ("orders.py", "payments.py")),
                degraded=self.kw.get("relevant_degraded", False),
            )

        # ponytail: every backend exposes graph_path() — used by the builder
        # to mention the index location in the prompt without knowing the
        # file naming convention itself.
        def graph_path(self, project_path):
            return project_path / "graphify-out" / "graph.json"

    return Stub(**overrides)


# ---- entity extraction ----------------------------------------------------


def test_extract_entities_keeps_camelcase_and_snake_case():
    builder = RepositoryContextBuilder(
        RepositoryKnowledge(_stub_backend(), project_path=Path("/tmp")),
    )
    item = {
        "fields": {
            "System.Title": "OrderService rejects invalid HttpClient instances",
            "System.Description": "OrderService.process fails when called from OrderController",
        }
    }
    entities = builder._extract_entities(item)
    assert "OrderService" in entities
    assert "HttpClient" in entities
    assert "OrderController" in entities


def test_extract_entities_drops_german_stopwords():
    """German article words are noise — drop them. The runtime example
    was: "Ordering gesperrter Tag ist div" → must NOT surface entities
    like "Ordering", "gesperrter", "Tag", "ist", "div"."""
    builder = RepositoryContextBuilder(
        RepositoryKnowledge(_stub_backend(), project_path=Path("/tmp")),
    )
    item = {"fields": {"System.Title": "Ordering gesperrter Tag ist div"}}
    entities = builder._extract_entities(item)
    assert "Ordering" not in entities
    assert "gesperrter" not in entities
    assert "Tag" not in entities
    assert "ist" not in entities
    assert "div" not in entities


def test_extract_entities_drops_short_tokens():
    """Anything shorter than 4 chars (incl. punctuation-cleaned) is dropped."""
    builder = RepositoryContextBuilder(
        RepositoryKnowledge(_stub_backend(), project_path=Path("/tmp")),
    )
    item = {"fields": {"System.Title": "FooAbc BarBiz BazQux Ab AbcXyz"}}
    entities = builder._extract_entities(item)
    assert "Ab" not in entities  # 2 chars
    assert "Baz" not in entities  # 3 chars even with capital
    assert "FooAbc" in entities  # capital marker
    assert "AbcXyz" in entities
    # Internal camelCase atoms also surface
    assert "FooAbc".lower() in {e.lower() for e in entities}


def test_extract_entities_caps_at_max():
    builder = RepositoryContextBuilder(
        RepositoryKnowledge(_stub_backend(), project_path=Path("/tmp")),
        max_entities=2,
    )
    item = {"fields": {"System.Title": "FooBar BazQux AlphaOne TwoTwo"}}
    entities = builder._extract_entities(item)
    assert len(entities) == 2


def test_extract_entities_handles_missing_fields():
    builder = RepositoryContextBuilder(
        RepositoryKnowledge(_stub_backend(), project_path=Path("/tmp")),
    )
    assert builder._extract_entities({}) == []
    assert builder._extract_entities({"fields": {}}) == []


# ---- build() --------------------------------------------------------------


def test_build_renders_clean_prompt_section(tmp_path):
    backend = _stub_backend()
    knowledge = RepositoryKnowledge(backend, project_path=tmp_path)
    builder = RepositoryContextBuilder(knowledge)
    item = {"fields": {"System.Title": "OrderService rejects bad inputs"}}
    ctx = builder.build(item)
    section = ctx.to_prompt_section()
    # All curated content + graphify skill instructions present.
    assert "Architecture summary" in section
    assert "summary text" in section
    assert "Module dependency graph" in section
    assert "/graphify query" in section
    assert "/graphify path" in section
    assert "/graphify explain" in section
    assert "/graphify affected" in section
    # No stale "degraded fallback" misleading tag.
    assert "(degraded fallback)" not in section
    # The misleading "(no execution path found)" entry from the previous
    # version must be gone — execution_path is the agent's job now.
    assert "no execution path found" not in section
    assert "Impact analysis" not in section


def test_build_handles_no_entities(tmp_path):
    backend = _stub_backend()
    knowledge = RepositoryKnowledge(backend, project_path=tmp_path)
    builder = RepositoryContextBuilder(knowledge)
    ctx = builder.build({"fields": {"System.Title": ""}})
    assert ctx.relevant_files is None


def test_build_swallows_backend_errors(tmp_path):
    """A misbehaving backend must not break the prompt — section still renders."""

    class FlakyBackend(KnowledgeBackend):
        def status(self, project_path): return {"backend": "flaky", "ok": True}
        def search_text(self, q, *, project_path, top_k=50): return []
        def find_symbol(self, name, *, project_path, kind=None): return []
        def find_callers(self, symbol, *, project_path): return []
        def find_callees(self, symbol, *, project_path): return []
        def find_references(self, name, *, project_path): return []
        def find_implementations(self, name, *, project_path): return []
        def impact_analysis(self, symbol, *, project_path, depth=2): return {}
        def architecture_summary(self, *, project_path):
            raise RuntimeError("graphify offline")
        def dependency_graph(self, *, project_path):
            raise OSError("disk gone")
        def relevant_files(self, query, *, project_path, top_k=20):
            raise ValueError("bad query")
        def graph_path(self, project_path):
            return project_path / "graphify-out" / "graph.json"

    knowledge = RepositoryKnowledge(FlakyBackend(), project_path=tmp_path)
    builder = RepositoryContextBuilder(knowledge)
    ctx = builder.build({"fields": {"System.Title": "Foo"}})
    section = ctx.to_prompt_section()
    # Prompt still renders — no exception.
    assert "Repository context (curated)" in section


def test_graph_path_is_reported_when_index_exists(tmp_path):
    """When graph.json exists on disk, the prompt mentions its location."""
    backend = _stub_backend()
    # The stub's graph_path() points at graphify-out/graph.json — create it.
    (tmp_path / "graphify-out").mkdir()
    (tmp_path / "graphify-out" / "graph.json").write_text("{}")
    knowledge = RepositoryKnowledge(backend, project_path=tmp_path)
    builder = RepositoryContextBuilder(knowledge)
    ctx = builder.build({"fields": {"System.Title": "Foo"}})
    section = ctx.to_prompt_section()
    assert "Graph index ready at" in section
    assert str(tmp_path) in section


def test_graph_path_advisory_when_index_missing(tmp_path):
    """If graph.json is absent, the prompt tells the agent to populate it
    or call the skill — no false reassurance."""
    backend = _stub_backend()
    knowledge = RepositoryKnowledge(backend, project_path=tmp_path)
    builder = RepositoryContextBuilder(knowledge)
    ctx = builder.build({"fields": {"System.Title": "Foo"}})
    section = ctx.to_prompt_section()
    assert "Graph index not built" in section