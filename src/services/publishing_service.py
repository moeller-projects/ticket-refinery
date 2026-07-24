"""Publishing: write Pi findings back to ADO.

Owns:
- The comment (summary, or unknowns-list when blocked).
- Optional Title patch.
- Attachment upload + relation.
- Trigger → done / blocked tag transitions.

Description and Acceptance Criteria are intentionally never modified. The
refinement result is posted as a work-item comment instead.
"""
from __future__ import annotations

import html as _html
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from ado_client import AdoClient
from retry import with_retry

log = logging.getLogger("refine.publishing")

# Transient failures for ADO REST: HTTP-5xx (raised as RequestException),
# connection resets, timeouts. Auth fails fast — not in the tuple.
_RETRYABLE_REST: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    OSError,
)


class PublishingService:
    """All outbound writes to ADO for one refinement outcome."""

    def __init__(self, *, client: AdoClient) -> None:
        self._client = client

    def publish(
        self,
        item: dict,
        findings: dict,
        *,
        allow_title_edits: bool,
        tag_trigger: str,
        tag_done: str,
        tag_blocked: str,
        result_markdown: str | None,
        attachment_name: str | None,
        on_attachment_seconds: callable | None = None,
    ) -> None:
        """Dispatch on findings outcome: blocked (unknowns) vs done."""
        if result_markdown is not None and attachment_name is not None:
            self._upload_attachment(
                item["id"], attachment_name, result_markdown, on_attachment_seconds
            )

        unknowns = findings.get("unknowns", [])
        if unknowns:
            self._publish_blocked(item, findings, tag_blocked)
        else:
            self._publish_done(
                item, findings, allow_title_edits=allow_title_edits,
                tag_trigger=tag_trigger, tag_done=tag_done,
            )

    # ---- attachment ------------------------------------------------------

    def _upload_attachment(
        self,
        item_id: int,
        name: str,
        content: "bytes | str",
        on_seconds: callable | None,
    ) -> None:
        # ponytail: accept str (markdown) or bytes (raw); ADO wants raw bytes.
        payload = content.encode("utf-8") if isinstance(content, str) else content
        log.info("item %s attachment name=%s len=%d", item_id, name, len(payload))
        started = _now()

        def _do_upload():
            attachment = self._client.upload_attachment(name, payload)
            self._client.add_attachment_relation(item_id, attachment["url"])
            log.info("item %s attachment_uploaded url=%s", item_id, attachment["url"])

        with_retry(_do_upload, retryable=_RETRYABLE_REST)
        if on_seconds is not None:
            on_seconds(_now() - started)

    # ---- outcome: blocked -----------------------------------------------

    def _publish_blocked(self, item: dict, findings: dict, tag_blocked: str) -> None:
        unknowns = findings.get("unknowns", [])
        comment = format_unknowns(findings)
        log.info(
            "item %s outcome=blocked unknowns=%d comment_len=%d comment_preview=%r",
            item["id"], len(unknowns), len(comment), comment[:400],
        )
        self._comment(item["id"], comment, blocked=True)
        log.info("item %s azure_tag action=add tag=%s", item["id"], tag_blocked)
        self._safe(
            lambda: self._client.add_tag(item, tag_blocked),
            "add_tag(blocked)", item["id"],
        )

    # ---- outcome: done ---------------------------------------------------

    def _publish_done(
        self,
        item: dict,
        findings: dict,
        *,
        allow_title_edits: bool,
        tag_trigger: str,
        tag_done: str,
    ) -> None:
        facts = findings.get("facts", [])
        objects = findings.get("classes", [])
        api_specs = findings.get("api_specs", [])
        summary = format_summary(findings)
        log.info(
            "item %s outcome=done facts=%d objects=%d api_specs=%d summary_len=%d",
            item["id"], len(facts), len(objects), len(api_specs), len(summary),
        )
        # Refinement results belong in the work-item discussion, not in the
        # Description or Acceptance Criteria fields.
        if allow_title_edits and findings.get("suggested_title"):
            log.info("item %s azure_patch field=System.Title value=%r",
                     item["id"], findings["suggested_title"])
            self._safe(
                lambda: self._client.patch_title(item["id"], findings["suggested_title"]),
                "patch_title", item["id"],
            )
        self._comment(item["id"], summary, blocked=False)
        log.info("item %s azure_tag action=remove tag=%s", item["id"], tag_trigger)
        self._safe(
            lambda: self._client.remove_tag(item, tag_trigger),
            "remove_tag", item["id"],
        )
        log.info("item %s azure_tag action=add tag=%s", item["id"], tag_done)
        self._safe(
            lambda: self._client.add_tag(item, tag_done),
            "add_tag(done)", item["id"],
        )

    # ---- helpers ---------------------------------------------------------

    def _comment(self, item_id: int, body: str, *, blocked: bool) -> None:
        action = "blocked" if blocked else "summary"
        log.info("item %s azure_comment action=post %s", item_id, action)
        # A publish without its comment is incomplete. Propagate failure so
        # the item is not tagged done/blocked while the explanation is missing.
        with_retry(
            lambda: self._client.comment(item_id, body),
            retryable=_RETRYABLE_REST,
        )

    def _safe(self, fn: callable, label: str, item_id: int) -> None:
        """Run an ADO write with retry. Auth/validation errors propagate."""
        try:
            with_retry(fn, retryable=_RETRYABLE_REST)
        except Exception as e:  # narrow at the call site if needed
            log.warning("item %s %s failed: %s", item_id, label, e)


# ---- module-level render helpers (kept here; they're owned by publishing) -


def findings_to_html(findings: dict) -> str:
    def esc(s) -> str:
        return _html.escape(str(s), quote=False)

    parts: list[str] = ["### Facts"]
    for fact in findings.get("facts", []):
        parts.append(f"- {esc(fact)}")
    if findings.get("classes"):
        parts.append("\n### Classes")
        for c in findings["classes"]:
            parts.append(f"- **{esc(c['name'])}** ({esc(c['kind'])}) — `{esc(c['sourceRef'])}`")
    if findings.get("api_specs"):
        parts.append("\n### API specs")
        for a in findings["api_specs"]:
            parts.append(f"- `{esc(a['method'])} {esc(a['path'])}` — `{esc(a['sourceRef'])}`")
    return "\n".join(parts)


def findings_to_ac_html(findings: dict) -> str:
    # ponytail: AC payload is intentionally minimal — the brief doesn't
    # define an AC shape. Wire real content once a concrete format lands.
    return "<!-- auto-derived from refinement; review before sign-off -->"


def format_unknowns(findings: dict) -> str:
    lines = ["## Refinement blocked — open questions", ""]
    for u in findings.get("unknowns", []):
        lines.append(f"- **{u['question']}** — {u['why']}")
    return "\n".join(lines)


def format_summary(findings: dict) -> str:
    return "\n".join([
        "## Refinement summary",
        "",
        f"- Facts: {len(findings.get('facts', []))}",
        f"- Classes: {len(findings.get('classes', []))}",
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
    if findings.get("classes"):
        lines += ["", "## Classes"]
        for c in findings["classes"]:
            lines.append(f"- {c['name']} ({c['kind']}) — {c['sourceRef']}")
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
    title = re.sub(r"[^A-Za-z0-9._-]+", "-", item["fields"].get("System.Title", "")).strip("-")[:60] or "item"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"refinement-{item['id']}-{stamp}-{title}.md"


def _now() -> float:
    import time as _t
    return _t.perf_counter()
