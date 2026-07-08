"""Refinement workflow for one work item.

Coordinates the per-item phase order:

    WorkspaceService.prepare
        ↓
    ContextService.build_inputs    (comments + render)
        ↓
    Pi (via pi_runner)             ← retried
        ↓
    validate.check                  (NOT retried — schema/ref-resolution failures are permanent)
        ↓
    PublishingService.publish       (retried per-write)
        ↓
    WorkspaceService.cleanup        (always, in finally)

Public surface is `refine(item)` plus a thin `process_item` wrapper kept
for backwards compatibility (existing tests + `refine.process_item` import).
"""
from __future__ import annotations

import logging
from pathlib import Path

from ado_client import AdoClient
import pi_runner
from services.context_service import ContextService
from services.publishing_service import (
    PublishingService,
    build_result_markdown,
    versioned_attachment_name,
)
from services.workspace_service import WorkspaceService
import validate as validate_module

log = logging.getLogger("refine")


class RefinementService:
    """Per-item orchestrator. Lifecycle: one instance per process, reused."""

    def __init__(
        self,
        *,
        cfg,  # refine.Config — keep loose to avoid circular import
        client: AdoClient,
        repos_map: dict,
        workspace_service: WorkspaceService,
        context_service: ContextService,
        publishing_service: PublishingService,
        metrics=None,  # optional MetricsCollector
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._repos_map = repos_map
        self._workspace = workspace_service
        self._context = context_service
        self._publishing = publishing_service
        self._metrics = metrics

    def refine(self, item: dict) -> None:
        item_id = item["id"]
        # 1. Resolve repos
        repos = self._resolve_repos(item)
        # 2. Workspace preparation (with timing)
        with self._timer("workspace_preparation_seconds"):
            workspace = self._workspace.prepare(
                item_id, repos, self._cfg.clone_depth, self._cfg.ado_pat,
                on_clone_duration=self._record_duration("clone_seconds"),
            )
        try:
            # 3. Context + prompt
            with self._timer("prompt_generation_seconds"):
                prompt = self._context.build_inputs(
                    item, [r["name"] for r in repos], workspace.path,
                )
            log.info("item %s prompt_len=%d", item_id, len(prompt))

            # 4. Pi (retried by pi_runner for transient subprocess failures)
            with self._timer("pi_execution_seconds"):
                findings = pi_runner.run(prompt, self._cfg.pi_model)
            log.info("item %s findings keys=%s", item_id, sorted(findings.keys()))

            # 5. Validation — PERMANENT failure mode, no retry.
            with self._timer("validation_seconds"):
                validate_module.check(
                    findings, workspace.path,
                    Path(__file__).resolve().parent.parent / "schema" / "findings.schema.json",
                    known_repos=[r["name"] for r in repos],
                )

            # 6. Publishing (queued attachment + per-write retry inside)
            result_md = build_result_markdown(item, findings)
            attachment_name = versioned_attachment_name(item)
            with self._timer("publishing_seconds"):
                self._publishing.publish(
                    item, findings,
                    allow_title_edits=self._cfg.allow_title_edits,
                    tag_trigger=self._cfg.tag_trigger,
                    tag_done=self._cfg.tag_done,
                    tag_blocked=self._cfg.tag_blocked,
                    result_markdown=result_md,
                    attachment_name=attachment_name,
                    on_attachment_seconds=self._record_duration("attachment_upload_seconds"),
                )
            self._increment("successful_refinements_total")
        except pi_runner.InfraError:
            self._increment("infra_failures_total")
            raise
        except Exception:
            self._increment("blocked_refinements_total")
            raise
        finally:
            self._workspace.cleanup(workspace.path)

    def _resolve_repos(self, item: dict) -> list[dict]:
        tags = item["fields"].get("System.Tags", "") or ""
        repo_tags = [t.split(":", 1)[1] for t in tags.split("; ") if t.startswith("repo:")]
        missing = [t for t in repo_tags if t not in self._repos_map]
        if missing:
            raise pi_runner.InfraError(
                f"repo:<name> tags not in repos.jsonc: {missing}"
            )
        return [{"name": n, **self._repos_map[n]} for n in repo_tags]

    # ---- metrics helpers ------------------------------------------------

    def _timer(self, name: str):
        if self._metrics is None:
            return _NullTimer()
        return self._metrics.timer(name)

    def _record_duration(self, name: str):
        if self._metrics is None:
            return None
        col = self._metrics

        def _on(seconds: float) -> None:
            col.increment(f"{name}_samples_total")
            with col.timer(name):
                pass  # no-op timer to register a datapoint; below replaces per-call timing
        # Use a one-shot timer for each phase by wrapping in a context manager.
        # Simpler: just record timer once via context manager in caller. Here we
        # expose a callable that takes a duration.
        def _record(seconds: float) -> None:
            col.increment(f"{name}_samples_total")
            col._timings_ms.setdefault(name, []).append(seconds * 1000.0)  # ponytail: private but only this class writes it
        return _record

    def _increment(self, name: str) -> None:
        if self._metrics is not None:
            self._metrics.increment(name)


class _NullTimer:
    """Stand-in for MetricsCollector.timer when metrics are disabled."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False
