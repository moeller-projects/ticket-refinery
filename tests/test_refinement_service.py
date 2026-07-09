"""RefinementService: phase orchestration with all collaborators mocked."""
import logging
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import pi_runner
from repository_knowledge import (
    ArchitectureSummary,
    DependencyGraph,
    ExecutionPath,
    KnowledgeBackend,
    ReferenceHit,
    RelevantFiles,
    RepositoryKnowledge,
)
from services.refinement_service import RefinementService
from services.workspace_service import Workspace
import validate as validate_module


def _cfg(**overrides):
    @dataclass
    class C:
        ado_org = "o"; ado_project = "p"; ado_pat = "pat"
        tag_trigger = "needs-refinement"; tag_done = "refinement-done"; tag_blocked = "refinement-blocked"
        allow_title_edits = False; clone_depth = 1; pi_model = "m"; target_language = "English"

    return C(**{**C().__dict__, **overrides})


def _item():
    return {"id": 9, "fields": {"System.Tags": "repo:alpha; needs-refinement",
                                "System.Title": "OrderService"}}


@pytest.fixture
def services(monkeypatch):
    """Wire a RefinementService with all collaborators stubbed. Returns refs."""
    workspace_mock = MagicMock()
    workspace_mock.prepare.return_value = Workspace(
        path=Path("/tmp/refine-9"), repo_names=("alpha",),
    )
    workspace_mock.cleanup = MagicMock()

    context_mock = MagicMock()
    context_mock.build_inputs.return_value = "PROMPT"

    publishing_mock = MagicMock()
    client_mock = MagicMock()

    svc = RefinementService(
        cfg=_cfg(),
        client=client_mock,
        repos_map={"alpha": {"url": "u", "defaultBranch": "main"}},
        workspace_service=workspace_mock,
        context_service=context_mock,
        publishing_service=publishing_mock,
    )
    return svc, workspace_mock, context_mock, publishing_mock, client_mock


def _stub_knowledge() -> RepositoryKnowledge:
    class Stub(KnowledgeBackend):
        def status(self, project_path): return {"backend": "stub", "ok": True}
        def search_text(self, q, *, project_path, top_k=50): return []
        def find_symbol(self, n, *, project_path, kind=None): return []
        def find_callers(self, s, *, project_path): return []
        def find_callees(self, s, *, project_path): return []
        def find_references(self, n, *, project_path): return []
        def find_implementations(self, n, *, project_path): return []
        def impact_analysis(self, s, *, project_path, depth=2): return {}
        def architecture_summary(self, *, project_path):
            return ArchitectureSummary(text="stub arch", modules=("alpha",))
        def dependency_graph(self, *, project_path):
            return DependencyGraph()
        def execution_path(self, symbol, *, project_path):
            return ExecutionPath(symbol=symbol)
        def relevant_files(self, query, *, project_path, top_k=20):
            return RelevantFiles(query=query, files=("a.py",))

    return RepositoryKnowledge(Stub(), project_path=Path("/tmp/refine-9"))


def test_refine_runs_all_phases_in_order(services, monkeypatch):
    svc, workspace, context, publishing, _ = services
    monkeypatch.setattr(pi_runner, "run", lambda *a, **kw: {
        "facts": [], "dtos": [], "api_specs": [], "unknowns": [], "sourceRefs": [],
        "suggested_title": "X",
    })
    monkeypatch.setattr(validate_module, "check", lambda *a, **kw: None)
    svc.refine(_item())

    workspace.prepare.assert_called_once()
    context.build_inputs.assert_called_once()
    publishing.publish.assert_called_once()
    workspace.cleanup.assert_called_once_with(Path("/tmp/refine-9"))


def test_refine_runs_cleanup_in_finally_after_infraerror(services, monkeypatch):
    svc, workspace, context, publishing, _ = services

    def boom(*a, **kw):
        raise pi_runner.InfraError("auth failed")

    monkeypatch.setattr(pi_runner, "run", boom)
    monkeypatch.setattr(validate_module, "check", lambda *a, **kw: None)
    with pytest.raises(pi_runner.InfraError):
        svc.refine(_item())
    workspace.cleanup.assert_called_once_with(Path("/tmp/refine-9"))
    publishing.publish.assert_not_called()


def test_refine_skips_when_no_repos_tagged(services):
    svc, *_ = services
    item = {"id": 9, "fields": {"System.Tags": "needs-refinement"}}  # no repo:
    with pytest.raises(pi_runner.InfraError):
        svc.refine(item)


def test_refine_uses_metrics_when_provided(services, monkeypatch):
    svc, workspace, context, publishing, _ = services
    import metrics
    col = metrics.MetricsCollector()
    svc._metrics = col
    monkeypatch.setattr(pi_runner, "run", lambda *a, **kw: {
        "facts": [], "dtos": [], "api_specs": [], "unknowns": [], "sourceRefs": [],
    })
    monkeypatch.setattr(validate_module, "check", lambda *a, **kw: None)
    svc.refine(_item())
    snap = col.snapshot()
    assert "workspace_preparation_seconds" in snap.timings_ms
    assert "pi_execution_seconds" in snap.timings_ms
    assert "validation_seconds" in snap.timings_ms
    assert "publishing_seconds" in snap.timings_ms
    assert snap.counters.get("successful_refinements_total") == 1


def test_refine_increments_blocked_counter_on_validation_failure(services, monkeypatch):
    svc, *_ = services
    import metrics
    col = metrics.MetricsCollector()
    svc._metrics = col
    monkeypatch.setattr(pi_runner, "run", lambda *a, **kw: {
        "facts": [], "dtos": [], "api_specs": [], "unknowns": [], "sourceRefs": [],
    })

    def _bad(*a, **kw):
        raise validate_module.ValidationError("schema fail")

    monkeypatch.setattr(validate_module, "check", _bad)
    with pytest.raises(validate_module.ValidationError):
        svc.refine(_item())
    snap = col.snapshot()
    assert snap.counters.get("blocked_refinements_total") == 1


def test_refine_records_attachment_upload_when_provided(services, monkeypatch):
    """When publishing_service invokes the on_attachment_seconds callback, the
    RefinementService forwards the duration into the metrics collector.
    Verified here at the service boundary using a fake callback."""
    svc, *_ = services
    import metrics
    col = metrics.MetricsCollector()
    svc._metrics = col

    # Wire the publishing mock to invoke the duration callback immediately.
    def fake_publish(item, findings, *, on_attachment_seconds=None, **kw):
        if on_attachment_seconds is not None:
            on_attachment_seconds(0.123)

    publishing_mock = services[3]
    publishing_mock.publish.side_effect = fake_publish

    monkeypatch.setattr(pi_runner, "run", lambda *a, **kw: {
        "facts": [], "dtos": [], "api_specs": [], "unknowns": [], "sourceRefs": [],
    })
    monkeypatch.setattr(validate_module, "check", lambda *a, **kw: None)
    svc.refine(_item())
    snap = col.snapshot()
    assert "attachment_upload_seconds" in snap.timings_ms


def test_resolve_repos_extracts_tag_set():
    svc = RefinementService.__new__(RefinementService)
    svc._repos_map = {"a": {"url": "u"}, "b": {"url": "u"}}
    repos = svc._resolve_repos({"id": 1, "fields": {"System.Tags": "repo:a; repo:b; noise"}})
    assert {r["name"] for r in repos} == {"a", "b"}


def test_resolve_repos_raises_for_unknown():
    svc = RefinementService.__new__(RefinementService)
    svc._repos_map = {"a": {"url": "u"}}
    with pytest.raises(pi_runner.InfraError, match="not in"):
        svc._resolve_repos({"id": 1, "fields": {"System.Tags": "repo:missing"}})


# ---- RepositoryKnowledge integration --------------------------------------


def test_refine_splices_repo_context_into_prompt(monkeypatch):
    """When a knowledge instance is injected, the orchestrator builds a
    curated RepositoryContext and passes it through to ContextService as the
    `repo_context_section` argument."""
    workspace_mock = MagicMock()
    workspace_mock.prepare.return_value = Workspace(
        path=Path("/tmp/refine-9"), repo_names=("alpha",),
    )
    workspace_mock.cleanup = MagicMock()
    context_mock = MagicMock()
    context_mock.build_inputs.return_value = "PROMPT"
    publishing_mock = MagicMock()

    svc = RefinementService(
        cfg=_cfg(),
        client=MagicMock(),
        repos_map={"alpha": {"url": "u", "defaultBranch": "main"}},
        workspace_service=workspace_mock,
        context_service=context_mock,
        publishing_service=publishing_mock,
        knowledge=_stub_knowledge(),
    )

    monkeypatch.setattr(pi_runner, "run", lambda *a, **kw: {
        "facts": [], "dtos": [], "api_specs": [], "unknowns": [], "sourceRefs": [],
    })
    monkeypatch.setattr(validate_module, "check", lambda *a, **kw: None)
    svc.refine(_item())

    # The curated section reaches ContextService.build_inputs.
    call = context_mock.build_inputs.call_args
    assert "repo_context_section" in call.kwargs
    assert "stub arch" in call.kwargs["repo_context_section"]


def test_refine_without_knowledge_skips_repo_context(monkeypatch):
    """Backwards compatibility: services built without `knowledge` still
    work — `build_inputs` is called with an empty repo_context_section."""
    workspace_mock = MagicMock()
    workspace_mock.prepare.return_value = Workspace(
        path=Path("/tmp/refine-9"), repo_names=("alpha",),
    )
    workspace_mock.cleanup = MagicMock()
    context_mock = MagicMock()
    context_mock.build_inputs.return_value = "PROMPT"
    publishing_mock = MagicMock()

    svc = RefinementService(
        cfg=_cfg(),
        client=MagicMock(),
        repos_map={"alpha": {"url": "u", "defaultBranch": "main"}},
        workspace_service=workspace_mock,
        context_service=context_mock,
        publishing_service=publishing_mock,
        # knowledge=None (default) — legacy wiring.
    )

    monkeypatch.setattr(pi_runner, "run", lambda *a, **kw: {
        "facts": [], "dtos": [], "api_specs": [], "unknowns": [], "sourceRefs": [],
    })
    monkeypatch.setattr(validate_module, "check", lambda *a, **kw: None)
    svc.refine(_item())
    call = context_mock.build_inputs.call_args
    assert call.kwargs.get("repo_context_section") == ""