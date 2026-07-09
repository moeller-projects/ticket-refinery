"""Context service: existing comments + work-item text + curated repo context → Pi prompt.

Responsibilities:
- Fetch existing ADO comments.
- Render the Pi prompt from the work item + comments + workspace.
- Optionally splice a curated `RepositoryContext` into the prompt when the
  orchestrator has already explored the repository (preferred path).
- Stay close to the existing `render_prompt` behaviour (same template,
  same placeholders) so output is backwards-compatible.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ado_client import AdoClient

log = logging.getLogger("refine.context")


class ContextService:
    """Load + assemble the inputs Pi needs for one item."""

    def __init__(
        self,
        *,
        client: AdoClient,
        schema_path: Path,
        prompt_path: Path,
        target_language: str = "English",
        comment_top: int = 20,
    ) -> None:
        self._client = client
        self._schema_path = schema_path
        self._prompt_path = prompt_path
        self._target_language = target_language
        self._comment_top = comment_top

    def load_comments(self, item_id: int) -> str:
        """Return prior comments formatted for the prompt body."""
        comments = self._client.get_comments(item_id, top=self._comment_top)
        log.info("item %s existing_comments=%d", item_id, len(comments))
        # ponytail: no leading `[...]` — the prompt template already wraps
        # the block, so adding it here nests visually and confuses the LLM.
        return "\n\n".join(
            f"{c.get('createdBy', {}).get('displayName', '?')} @ {c.get('createdDate', '?')}\n{c.get('text', '')}"
            for c in comments
        )

    def render_prompt(
        self,
        item: dict,
        repo_names: list[str],
        workspace: Path,
        comments_text: str,
        repo_context_section: str = "",
    ) -> str:
        """Render the refinement prompt for an item.

        `repo_context_section` is pre-rendered markdown for the curated
        repository context. Empty string ⇒ the prompt omits the context
        section (legacy behaviour).
        """
        schema_text = self._schema_path.read_text()
        tmpl = self._prompt_path.read_text()
        f = item["fields"]
        return (
            tmpl.replace("{workspace}", str(workspace))
            .replace("{title}", f.get("System.Title", ""))
            .replace("{description}", f.get("System.Description", "") or "")
            .replace("{acceptance_criteria}",
                     f.get("Microsoft.VSTS.Common.AcceptanceCriteria", "") or "")
            .replace("{system_info}",
                     f.get("Microsoft.VSTS.TCM.SystemInfo", "") or "")
            .replace("{repro_steps}",
                     f.get("Microsoft.VSTS.TCM.ReproSteps", "") or "")
            .replace("{comments}", comments_text)
            .replace("{repo_list}", ", ".join(repo_names))
            .replace("{target_language}", self._target_language)
            .replace("{schema}", schema_text)
            .replace("{repo_context}", repo_context_section)
        )

    def build_inputs(
        self,
        item: dict,
        repo_names: list[str],
        workspace: Path,
        repo_context_section: str = "",
    ) -> str:
        """Convenience: comments + prompt in one call. Equiv. of inlined
        behaviour inside the old orchestrator loop.

        `repo_context_section` is the pre-rendered curated repository
        context. Pass an empty string to render the prompt without it
        (legacy behaviour).
        """
        comments_text = self.load_comments(item["id"])
        return self.render_prompt(
            item, repo_names, workspace, comments_text,
            repo_context_section=repo_context_section,
        )