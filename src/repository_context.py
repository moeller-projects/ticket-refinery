"""Repository context: a thin kickstart for the agent.

The orchestrator builds a small curated preamble from `RepositoryKnowledge`
so the agent has a launchpad (architecture overview, files most likely
relevant to the work item, dependency graph). Anything deeper — call paths,
impact analysis, semantic graph queries — is the agent's job: it can
invoke the Graphify skill directly via `/graphify query / path / explain /
affected`. The skill is installed at image-build time (`graphify pi install`)
and resolves against `<workspace>/graphify-out/graph.json` (built per repo
by `WorkspaceService._sync_graphify_indexes`).

Design contract:
- This module only sees `RepositoryKnowledge` (the abstraction).
- `GraphifyBackend` is the only place that imports graphify internals.
- All backend calls are defensive — a missing graph or CLI failure should
  never break the prompt; we degrade the rendered block in place.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from repository_knowledge import (
    ArchitectureSummary,
    KnowledgeBackend,
    RelevantFiles,
    RepositoryKnowledge,
)

log = logging.getLogger("refine.repo_context")

# ponytail: cheap keyword extraction. We only want code-shaped identifiers
# (camelCase / snake_case / PascalCase) — German article words like
# "gesperrter" or "Ordering" without a partner term produce noise when
# graphed. The heuristics below narrow the input enough that the
# curated-files section stays useful instead of empty for every noun.

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# Stopwords (lowercased). Includes German articles / common words so the
# entity extractor doesn't surface them as candidate symbols.
_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "have", "has",
    "are", "was", "were", "but", "not", "you", "your", "our", "their",
    "into", "when", "then", "than", "also", "any", "all", "its", "out",
    "what", "which", "who", "how", "why", "should", "would", "could",
    "need", "needs", "must", "item", "work", "ticket", "field",
    "fields", "value", "values", "type", "string", "number", "boolean",
    "true", "false", "null", "none",
    "ist", "sind", "war", "ein", "eine", "einer", "eines", "der",
    "die", "das", "den", "dem", "des", "und", "oder", "aber", "auch",
    "noch", "schon", "sehr", "wie", "was", "wer", "wo", "wann",
    "warum", "wieso", "weshalb", "bei", "von", "aus", "mit", "ohne",
    "nach", "seit", "wird", "wurde", "wurden", "nicht", "kein", "keine",
    "tags", "tag", "div",
}


def _looks_like_identifier(tok: str) -> bool:
    """True when `tok` has a structural marker of code identity.

    ponytail: pure-lowercase prose tokens like `gesperrter`, `ordering`,
    `erlaubnis` are real German/English words; querying the Graphify
    graph with them produces noise. Code-shaped tokens have an internal
    capital letter, an underscore, or a digit somewhere past position 0.
    """
    if "_" in tok:
        return True
    if any(c.isdigit() for c in tok[1:]):
        return True
    if any(c.isupper() for c in tok[1:]):
        return True
    return False


@dataclass(frozen=True)
class RepositoryContext:
    """Curated kickstart for one work item."""

    architecture: ArchitectureSummary
    relevant_files: RelevantFiles | None = None
    knowledge_backend: str = "unknown"
    graph_ready: bool = False
    graph_path: str | None = None

    def to_prompt_section(self) -> str:
        """Render as a markdown block ready for the Pi prompt.

        ponytail: this block should be small. The full dependency graph
        and large file lists live on disk; the prompt points at them via
        the Graphify skill cheatsheet. Rendering them inline bloats the
        prompt without helping the answer.
        """
        backend = self.knowledge_backend or "unknown"
        sections = ["## Repository context (curated)", ""]
        sections.append(
            "The application has indexed the repository and prepared a small "
            "context block as a launchpad. Reason over this content first; "
            "use `read` only on the files listed below to verify specific lines."
        )
        sections.append("")
        status_bits: list[str] = [f"_Knowledge backend: `{backend}`._"]
        if self.graph_ready and self.graph_path:
            status_bits.append(f"_Graph index at `{self.graph_path}`._")
        else:
            status_bits.append(
                "_Graph index not built — run `graphify extract --code-only` "
                "or invoke the Graphify skill to populate it._"
            )
        sections.append(" ".join(status_bits))
        sections.append("")

        sections.append("**For deeper exploration**, invoke the Graphify skill:")
        sections.append("")
        sections.append("- `/graphify query \"<question>\"` — semantic traversal "
                        "(e.g., `\"what connects auth to the database?\"`)")
        sections.append("- `/graphify path \"<A>\" \"<B>\"` — shortest path between two symbols")
        sections.append("- `/graphify explain \"<node>\"` — explanation of a node")
        sections.append("- `/graphify affected \"<symbol>\" --depth N` — impact analysis")
        sections.append("")

        sections.append("### Architecture summary")
        sections.append("")
        arch_text = self.architecture.text or "(no architecture summary available)"
        sections.append(arch_text)
        sections.append("")

        if self.relevant_files is not None and self.relevant_files.files:
            sections.append("### Relevant files")
            sections.append("")
            if self.relevant_files.query:
                sections.append(f"Query terms: `{self.relevant_files.query}`")
                sections.append("")
            for f in self.relevant_files.files:
                sections.append(f"- `{f}`")
            sections.append("")

        return "\n".join(sections)


class RepositoryContextBuilder:
    """Composes a small `RepositoryContext` from `RepositoryKnowledge`."""

    def __init__(
        self,
        knowledge: RepositoryKnowledge,
        *,
        max_entities: int = 4,
    ) -> None:
        self._knowledge = knowledge
        self._max_entities = max_entities

    @property
    def knowledge(self) -> RepositoryKnowledge:
        return self._knowledge

    def build(self, item: dict) -> RepositoryContext:
        entities = self._extract_entities(item)
        project_path = self._knowledge.project_path

        architecture = self._safe_architecture()
        relevant = self._safe_relevant(entities)

        graph_ready, graph_path = self._graph_status(project_path)

        return RepositoryContext(
            architecture=architecture,
            relevant_files=relevant,
            knowledge_backend=self._knowledge.backend_name,
            graph_ready=graph_ready,
            graph_path=graph_path,
        )

    # ---- extraction -----------------------------------------------------

    def _extract_entities(self, item: dict) -> list[str]:
        """Pull code-shaped identifiers from the work item.

        Acceptance criteria for a candidate token:
        1. ≥ 4 characters.
        2. NOT a stopword.
        3. Has a structural marker of code: an internal capital letter, an
           underscore, OR a digit. Pure-lowercase prose (`gesperrter`,
           `ordering`) is dropped — it's article / German / non-symbolic.
           `OrderService`, `order_service`, `http2`, `OrderController` all
           survive.
        4. camelCase / snake_case atoms are surfaced as fall-backs so a
           long identifier doesn't drop its parts.
        """
        f = item.get("fields", {}) if isinstance(item, dict) else {}
        chunks = [
            f.get("System.Title", ""),
            f.get("System.Description", "") or "",
            f.get("Microsoft.VSTS.Common.AcceptanceCriteria", "") or "",
            f.get("Microsoft.VSTS.TCM.ReproSteps", "") or "",
            f.get("Microsoft.VSTS.TCM.SystemInfo", "") or "",
        ]
        haystack = " ".join(str(c) for c in chunks)
        candidates: list[str] = []
        for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", haystack):
            if len(tok) < 4:
                continue
            if tok.lower() in _STOPWORDS:
                continue
            if not _looks_like_identifier(tok):
                continue
            candidates.append(tok)
            for part in _CAMEL_BOUNDARY.split(tok):
                if (len(part) >= 5 and part.lower() not in _STOPWORDS
                        and _looks_like_identifier(part)):
                    candidates.append(part)
        seen: set[str] = set()
        out: list[str] = []
        for cand in candidates:
            if cand.lower() in seen:
                continue
            seen.add(cand.lower())
            out.append(cand)
            if len(out) >= self._max_entities:
                break
        return out

    # ---- safe wrappers (never break the prompt) -------------------------

    def _safe_architecture(self) -> ArchitectureSummary:
        try:
            return self._knowledge.architecture_summary()
        except Exception as e:  # noqa: BLE001 — graceful degradation is the point
            log.warning("architecture_summary failed: %s", e)
            return ArchitectureSummary(text="(architecture summary unavailable)",
                                        modules=(), degraded=True)

    def _safe_relevant(self, entities: list[str]) -> RelevantFiles | None:
        if not entities:
            return None
        query = " ".join(entities[:4])
        try:
            return self._knowledge.relevant_files(query)
        except Exception as e:  # noqa: BLE001
            log.warning("relevant_files failed: %s", e)
            return RelevantFiles(query=query, degraded=True)

    def _graph_status(self, project_path) -> tuple[bool, str | None]:
        """Probe whether the Graphify index exists for this project.

        Recognises both layouts:
        - Single-repo: graph.json at `<project>/graphify-out/graph.json`
        - Workspace: graph.json at `<project>/<repo>/graphify-out/graph.json`
          for each cloned repo (the typical runtime case from
          WorkspaceService._sync_graphify_indexes).

        Reports the first graph found; the prompt only needs one to point at.
        """
        backend = self._knowledge.backend
        if hasattr(backend, "graph_path"):
            direct = backend.graph_path(project_path)
            if direct.exists():
                return True, str(direct)
        if hasattr(backend, "_discover_repo_graphs"):
            candidates = backend._discover_repo_graphs(project_path)
            if candidates:
                return True, str(candidates[0][1])
        return False, None
