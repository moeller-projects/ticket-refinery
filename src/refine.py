#!/usr/bin/env python3
"""Orchestrator: poll ADO → clone → Pi → validate → write back."""
from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

from ado_client import AdoClient
import git_ops
import pi_runner
import validate

ROOT = Path(__file__).parent
SCHEMA = ROOT / "schema" / "findings.schema.json"
PROMPT = ROOT / "prompts" / "refine.prompt.tmpl.md"
REPOS_CFG = ROOT / "repos.jsonc"

REQUIRED_ENV = [
    "ADO_ORG", "ADO_PROJECT",
    "TAG_TRIGGER", "TAG_DONE", "TAG_BLOCKED",
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
    target_language: str

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
            allow_title_edits=_clean(os.environ.get("ALLOW_TITLE_EDITS", "false")).lower() == "true",
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


def render_prompt(item: dict, repo_names: list[str], workspace: Path, comments_text: str = "", target_language: str = "English") -> str:
    schema_text = SCHEMA.read_text()
    tmpl = PROMPT.read_text()
    f = item["fields"]
    return (
        tmpl
        .replace("{workspace}", str(workspace))
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
        .replace("{target_language}", target_language)
        .replace("{schema}", schema_text)
    )


def findings_to_html(findings: dict) -> str:
    """Render findings to ADO description HTML.

    Escape user-derived strings so an `<!--` in a fact can't shadow our marker
    block (the regex in `_replace_block` would otherwise pick the wrong bound).
    ADO description is HTML, so this is safe; markdown structure (`###`, `-`)
    has no `<`/`>` to escape.
    """
    def esc(s) -> str:
        return html.escape(str(s), quote=False)
    parts: list[str] = ["### Facts"]
    for fact in findings.get("facts", []):
        parts.append(f"- {esc(fact)}")
    if findings.get("dtos"):
        parts.append("\n### DTOs")
        for d in findings["dtos"]:
            parts.append(f"- **{esc(d['name'])}** — `{esc(d['sourceRef'])}`")
    if findings.get("api_specs"):
        parts.append("\n### API specs")
        for a in findings["api_specs"]:
            parts.append(f"- `{esc(a['method'])} {esc(a['path'])}` — `{esc(a['sourceRef'])}`")
    return "\n".join(parts)


def findings_to_ac_html(findings: dict) -> str:
    # ponytail: AC payload is intentionally minimal — the brief doesn't define an AC shape.
    # Wire real content once a concrete AC format is agreed with the team.
    return "<!-- auto-derived from refinement; review before sign-off -->"


def format_unknowns(findings: dict) -> str:
    lines = ["## Refinement blocked — open questions", ""]
    for u in findings["unknowns"]:
        lines.append(f"- **{u['question']}** — {u['why']}")
    return "\n".join(lines)


def format_summary(findings: dict) -> str:
    return "\n".join([
        "## Refinement summary",
        "",
        f"- Facts: {len(findings.get('facts', []))}",
        f"- DTOs: {len(findings.get('dtos', []))}",
        f"- API specs: {len(findings.get('api_specs', []))}",
        f"- Source refs: {len(findings.get('sourceRefs', []))}",
    ])


def build_result_markdown(item: dict, findings: dict) -> str:
    lines = [
        f"# Refinement result for #{item['id']}",
        "",
        f"## Title\n{item['fields'].get('System.Title', '')}",
        "",
        format_summary(findings),
    ]
    if findings.get("facts"):
        lines += ["", "## Facts", *[f"- {f}" for f in findings["facts"]]]
    if findings.get("dtos"):
        lines += ["", "## DTOs"]
        for d in findings["dtos"]:
            lines.append(f"- {d['name']} — {d['sourceRef']}")
    if findings.get("api_specs"):
        lines += ["", "## API specs"]
        for a in findings["api_specs"]:
            lines.append(f"- {a['method']} {a['path']} — {a['sourceRef']}")
    if findings.get("unknowns"):
        lines += ["", "## Unknowns"]
        for u in findings["unknowns"]:
            lines.append(f"- {u['question']} — {u['why']}")
    return "\n".join(lines) + "\n"


def versioned_attachment_name(item: dict) -> str:
    title = re.sub(r"[^A-Za-z0-9._-]+", "-", item['fields'].get('System.Title', '')).strip('-')[:60] or 'item'
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"refinement-{item['id']}-{stamp}-{title}.md"


def _link_repo_cache(repos: list[dict], cache_root: Path, workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for repo in repos:
        src = cache_root / repo["name"]
        dst = workspace / repo["name"]
        if dst.exists():
            continue
        dst.symlink_to(src, target_is_directory=True)


def process_item(
    item: dict, cfg: Config, client: AdoClient, repos_map: dict, log: logging.Logger,
    *, repo_cache_root: Path | None = None,
) -> None:
    """Per-item workflow. Caller handles the surrounding loop + exit code."""
    workspace = Path(f"/tmp/refine-{item['id']}")
    cache_root = repo_cache_root or workspace
    try:
        repo_tags = extract_repo_tags(item)
        resolved = resolve_repos(repo_tags, repos_map)
        log.info("item %s repos=%s cache_root=%s workspace=%s", item['id'], [r['name'] for r in resolved], cache_root, workspace)
        git_ops.clone_all(resolved, cache_root, cfg.clone_depth, cfg.ado_pat)
        if cache_root != workspace:
            _link_repo_cache(resolved, cache_root, workspace)
        for repo in resolved:
            log.info("item %s repo_ready name=%s path=%s git=%s", item['id'], repo['name'], workspace / repo['name'], (workspace / repo['name'] / '.git').exists())

        comments = client.get_comments(item['id'], top=20)
        log.info("item %s existing_comments=%d", item['id'], len(comments))
        comments_text = "\n\n".join(
            f"[{c.get('createdBy', {}).get('displayName', '?')} @ {c.get('createdDate', '?')}]\n{c.get('text', '')}"
            for c in comments
        )

        prompt = render_prompt(item, [r["name"] for r in resolved], workspace, comments_text=comments_text, target_language=cfg.target_language)
        log.info("item %s prompt_len=%d", item['id'], len(prompt))
        findings = pi_runner.run(prompt, cfg.pi_model)
        log.info("item %s findings keys=%s", item['id'], sorted(findings.keys()))
        validate.check(findings, workspace, SCHEMA, known_repos=[r['name'] for r in resolved])

        result_md = build_result_markdown(item, findings)
        attach_name = versioned_attachment_name(item)
        log.info("item %s attachment name=%s len=%d", item['id'], attach_name, len(result_md))
        attachment = client.upload_attachment(attach_name, result_md.encode('utf-8'))
        client.add_attachment_relation(item['id'], attachment['url'])
        log.info("item %s attachment_uploaded url=%s", item['id'], attachment['url'])
        if findings.get("unknowns"):
            unknowns = findings.get('unknowns', [])
            comment = format_unknowns(findings)
            log.info("item %s outcome=blocked unknowns=%d comment_len=%d comment_preview=%r", item['id'], len(unknowns), len(comment), comment[:400])
            try:
                log.info("item %s azure_comment action=post blocked", item['id'])
                client.comment(item["id"], comment)
            except Exception as comment_err:
                log.warning("item %s comment failed: %s", item['id'], comment_err)
            log.info("item %s azure_tag action=add tag=%s", item['id'], cfg.tag_blocked)
            client.add_tag(item, cfg.tag_blocked)
        else:
            facts = findings.get('facts', [])
            dtos = findings.get('dtos', [])
            api_specs = findings.get('api_specs', [])
            desc_html = findings_to_html(findings)
            ac_html = findings_to_ac_html(findings)
            summary = format_summary(findings)
            log.info("item %s outcome=done facts=%d dtos=%d api_specs=%d desc_len=%d ac_len=%d summary_len=%d", item['id'], len(facts), len(dtos), len(api_specs), len(desc_html), len(ac_html), len(summary))
            log.info("item %s azure_patch field=System.Description len=%d", item['id'], len(desc_html))
            client.patch_description(item, desc_html)
            log.info("item %s azure_patch field=Microsoft.VSTS.Common.AcceptanceCriteria len=%d", item['id'], len(ac_html))
            client.patch_acceptance_criteria(item, ac_html)
            if cfg.allow_title_edits and findings.get("suggested_title"):
                log.info("item %s azure_patch field=System.Title value=%r", item['id'], findings['suggested_title'])
                client.patch_title(item["id"], findings["suggested_title"])
            try:
                log.info("item %s azure_comment action=post summary_len=%d preview=%r", item['id'], len(summary), summary[:400])
                client.comment(item["id"], summary)
            except Exception as comment_err:
                log.warning("item %s comment failed: %s", item['id'], comment_err)
            log.info("item %s azure_tag action=remove tag=%s", item['id'], cfg.tag_trigger)
            client.remove_tag(item, cfg.tag_trigger)
            log.info("item %s azure_tag action=add tag=%s", item['id'], cfg.tag_done)
            client.add_tag(item, cfg.tag_done)

    finally:
        git_ops.cleanup(workspace)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("refine")
    cfg = Config.from_env()
    client = AdoClient(cfg.ado_org, cfg.ado_project, cfg.ado_pat)
    repos_map = _load_jsonc(REPOS_CFG)  # ponytail: parse once, not per-item
    exit_code = 0
    repo_cache_root = Path(f"/tmp/refine-repos-{os.getpid()}")

    try:
        queue = client.query_items(cfg.tag_trigger, [cfg.tag_done, cfg.tag_blocked])
        log.info("queued items: %d", len(queue))

        for item in queue:
            try:
                process_item(item, cfg, client, repos_map, log, repo_cache_root=repo_cache_root)
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
        git_ops.cleanup(repo_cache_root)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())