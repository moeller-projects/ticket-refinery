#!/usr/bin/env python3
"""Orchestrator: poll ADO → for each tagged item → delegate to RefinementService.

This file is intentionally thin:
- Configuration + env loading (kept here because the run-script entry
  point lives in `main()`).
- Repo registry load + tag parsing.
- Backwards-compatible re-exports for tests + downstream imports
  (`findings_to_html`, `render_prompt`, `_link_repo_cache`, etc.).
- `process_item` wrapper that constructs the services and delegates to
  `RefinementService`. New code should not call it; prefer building the
  services once and calling `.refine(item)` directly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import git_ops  # noqa: F401  (re-imported as refine.git_ops by tests' monkeypatch)
import pi_runner
import validate  # noqa: F401  (re-imported as refine.validate by tests' monkeypatch)
from ado_client import AdoClient
from services.context_service import ContextService
from services.publishing_service import (
    PublishingService,
    build_result_markdown,
    findings_to_ac_html,
    findings_to_html,
    format_summary,
    format_unknowns,
    versioned_attachment_name,
)
from services.refinement_service import RefinementService
from services.workspace_service import WorkspaceService

ROOT = Path(__file__).parent
SCHEMA = ROOT / "schema" / "findings.schema.json"
PROMPT = ROOT / "prompts" / "refine.prompt.tmpl.md"
REPOS_CFG = ROOT / "repos.jsonc"

REQUIRED_ENV = [
    "ADO_ORG",
    "ADO_PROJECT",
    "TAG_TRIGGER",
    "TAG_DONE",
    "TAG_BLOCKED",
    "CLONE_DEPTH",
    "PI_MODEL",
]

# ASCII + Unicode quote chars (incl. smart quotes some Windows editors insert).
_QUOTE_CHARS = '"\u0027\u2018\u2019\u201c\u201d'


def _clean(value: str) -> str:
    """Strip surrounding quotes + trailing `# comment` + whitespace. Defends against:
    - CRLF line endings leaving a trailing \\r on Windows-edited .env files
    - Quoted values like CLONE_DEPTH="1" (podman's --env-file doesn't strip
      quotes the way docker's does)
    - Smart-quote autocorrect in Windows editors replacing ' with '\u2018'
    - Inline `# comments` from .env.example copy-paste — neither docker nor
      podman's --env-file strips inline `# ...`, so they end up in the value
      and break URL composition (`#` becomes a fragment, spaces get %20'd).
      Requirement: `#` must be preceded by whitespace, so values containing
      `#` (PATs, etc.) survive.
    """
    v = value.strip()
    # Strip trailing `# comment` FIRST so the quote-balance check below sees the
    # real value (e.g. `"myorg" # note` -> `"myorg"` -> `myorg`).
    v = re.sub(r"\s+#.*$", "", v)
    while len(v) >= 2 and v[0] in _QUOTE_CHARS and v[-1] in _QUOTE_CHARS:
        v = v[1:-1].strip()
    return v


@dataclass(frozen=True)
class Config:
    ado_org: str
    ado_project: str
    ado_pat: str
    tag_trigger: str
    tag_done: str
    tag_blocked: str
    allow_title_edits: bool
    clone_depth: int
    pi_model: str
    target_language: str = "English"

    @classmethod
    def from_env(cls) -> "Config":
        missing = [v for v in REQUIRED_ENV if not _clean(os.environ.get(v, ""))]
        if missing:
            raise SystemExit(
                f"Missing required env vars: {', '.join(missing)}. "
                f"Copy .env.example to .env and fill in values."
            )
        ado_pat = _clean(os.environ.get("ADO_PAT", ""))
        if not ado_pat:
            raise SystemExit("Missing ADO credentials: set ADO_PAT")
        return cls(
            ado_org=_clean(os.environ["ADO_ORG"]),
            ado_project=_clean(os.environ["ADO_PROJECT"]),
            ado_pat=ado_pat,
            tag_trigger=_clean(os.environ["TAG_TRIGGER"]),
            tag_done=_clean(os.environ["TAG_DONE"]),
            tag_blocked=_clean(os.environ["TAG_BLOCKED"]),
            allow_title_edits=_clean(
                os.environ.get("ALLOW_TITLE_EDITS", "false")
            ).lower()
            == "true",
            clone_depth=int(_clean(os.environ["CLONE_DEPTH"])),
            pi_model=_clean(os.environ["PI_MODEL"]),
            target_language=_clean(os.environ.get("TARGET_LANGUAGE", "English")),
        )


def _load_jsonc(path: Path) -> dict:
    # ponytail: stdlib regex strip — jsonc lib is overkill for one file.
    # Defenses:
    #   1. `//` must be preceded by whitespace/BOL (?<!: protects https:// in values)
    #   2. trailing commas before } or ] (JSONC allows, stdlib json doesn't)
    text = path.read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?<!\S)(?<!:)//[^\n]*", "", text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return json.loads(text)


def extract_repo_tags(item: dict) -> list[str]:
    tags = item["fields"].get("System.Tags", "") or ""
    return [t.split(":", 1)[1] for t in tags.split("; ") if t.startswith("repo:")]


def resolve_repos(repo_tags: list[str], repos_map: dict) -> list[dict]:
    missing = [t for t in repo_tags if t not in repos_map]
    if missing:
        raise pi_runner.InfraError(
            f"repo:<name> tags not in {REPOS_CFG.name}: {missing}"
        )
    return [{"name": n, **repos_map[n]} for n in repo_tags]


def render_prompt(
    item: dict,
    repo_names: list[str],
    workspace: Path,
    comments_text: str = "",
    target_language: str = "English",
) -> str:
    """Render the refinement prompt from item + repos + comments. Pure function.

    Kept as a module-level wrapper so the existing test surface works.
    The service equivalent is `services.context_service.ContextService.render_prompt`.
    """
    f = item["fields"]
    return (
        PROMPT.read_text()
        .replace("{workspace}", str(workspace))
        .replace("{title}", f.get("System.Title", ""))
        .replace("{description}", f.get("System.Description", "") or "")
        .replace(
            "{acceptance_criteria}",
            f.get("Microsoft.VSTS.Common.AcceptanceCriteria", "") or "",
        )
        .replace("{system_info}", f.get("Microsoft.VSTS.TCM.SystemInfo", "") or "")
        .replace("{repro_steps}", f.get("Microsoft.VSTS.TCM.ReproSteps", "") or "")
        .replace("{comments}", comments_text)
        .replace("{repo_list}", ", ".join(repo_names))
        .replace("{target_language}", target_language)
        .replace("{schema}", SCHEMA.read_text())
    )


# ---------------------------------------------------------------------------
# Backwards-compatible thin wrappers used by the existing test surface.
# The real implementations live in services.publishing_service.
# ---------------------------------------------------------------------------


def _link_repo_cache(repos: list[dict], cache_root: Path, workspace: Path) -> None:
    # Thin pass-through to the service. Kept here because the existing test
    # exercises `refine._link_repo_cache` directly.
    WorkspaceService._link_repo_cache(repos, cache_root, workspace)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def process_item(
    item: dict,
    cfg: Config,
    client: AdoClient,
    repos_map: dict,
    log: logging.Logger,
    *,
    repo_cache_root: Path | None = None,
) -> None:
    """Per-item workflow — thin wrapper around RefinementService.refine.

    Kept for backwards compatibility with the existing test surface. New code
    should construct the services once and call `refine(item)` directly.
    """
    cache_root = repo_cache_root or Path(f"/tmp/refine-repos-{os.getpid()}")
    workspace_svc = WorkspaceService(cache_root=cache_root)
    return
    context_svc = ContextService(
        client=client,
        schema_path=SCHEMA,
        prompt_path=PROMPT,
        target_language=cfg.target_language,
    )
    publishing_svc = PublishingService(client=client)
    refinement_svc = RefinementService(
        cfg=cfg,
        client=client,
        repos_map=repos_map,
        workspace_service=workspace_svc,
        context_service=context_svc,
        publishing_service=publishing_svc,
    )
    refinement_svc.refine(item)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    log = logging.getLogger("refine")
    cfg = Config.from_env()
    client = AdoClient(cfg.ado_org, cfg.ado_project, cfg.ado_pat)
    repos_map = _load_jsonc(REPOS_CFG)  # ponytail: parse once, not per-item
    exit_code = 0
    repo_cache_root = Path(f"/tmp/refine-repos-{os.getpid()}")

    # Build services once — they're cheap, stateless beyond config.
    workspace_svc = WorkspaceService(cache_root=repo_cache_root)
    context_svc = ContextService(
        client=client,
        schema_path=SCHEMA,
        prompt_path=PROMPT,
        target_language=cfg.target_language,
    )
    publishing_svc = PublishingService(client=client)
    refinement_svc = RefinementService(
        cfg=cfg,
        client=client,
        repos_map=repos_map,
        workspace_service=workspace_svc,
        context_service=context_svc,
        publishing_service=publishing_svc,
    )

    try:
        queue = client.query_items(cfg.tag_trigger, [cfg.tag_done, cfg.tag_blocked])
        log.info("queued items: %d", len(queue))

        for item in queue:
            try:
                refinement_svc.refine(item)
            except pi_runner.InfraError as e:
                log.error("item %s infra failure: %s", item["id"], e)
                exit_code = 1
            except validate.ValidationError as e:
                log.warning("item %s validation failed: %s", item["id"], e)
                # ponytail: validation failure = pipeline worked, content didn't.
                # Exit code stays 0 — a blocked tag is a successful signal.
                client.comment(item["id"], f"Refinement failed validation: {e}")
                client.add_tag(item, cfg.tag_blocked)
    finally:
        workspace_svc.shutdown()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
