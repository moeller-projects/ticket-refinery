"""PublishingService: writes back to ADO via the mocked AdoClient."""
from unittest.mock import MagicMock

import pytest

import pi_runner
from services.publishing_service import PublishingService, build_result_markdown, versioned_attachment_name


@pytest.fixture
def client():
    c = MagicMock()
    c.comment = MagicMock()
    c.add_tag = MagicMock()
    c.remove_tag = MagicMock()
    c.patch_description = MagicMock()
    c.patch_acceptance_criteria = MagicMock()
    c.patch_title = MagicMock()
    c.upload_attachment = MagicMock(return_value={"url": "https://example/att"})
    c.add_attachment_relation = MagicMock()
    return c


def _findings_done():
    return {
        "facts": ["one"],
        "classes": [{"name": "User", "kind": "class", "sourceRef": "r:u.py#L1", "fields": [], "methods": [], "relationships": []}],
        "api_specs": [{"method": "GET", "path": "/x", "sourceRef": "r:r.py#L1"}],
        "unknowns": [],
        "sourceRefs": ["r:u.py#L1"],
        "suggested_title": "Better T",
    }


def _findings_blocked():
    return {
        "facts": [],
        "classes": [],
        "api_specs": [],
        "unknowns": [{"question": "Why?", "why": "code unclear"}],
        "sourceRefs": [],
    }


def test_publish_blocked_writes_comment_and_adds_blocked_tag(client):
    svc = PublishingService(client=client)
    item = {"id": 7, "fields": {"System.Tags": "needs-refinement; repo:alpha"}}
    svc.publish(item, _findings_blocked(), allow_title_edits=True,
                tag_trigger="needs-refinement", tag_done="refinement-done",
                tag_blocked="refinement-blocked",
                result_markdown=None, attachment_name=None)
    client.comment.assert_called_once()
    client.add_tag.assert_called_once_with(item, "refinement-blocked")
    client.remove_tag.assert_not_called()


def test_publish_done_posts_comment_and_transitions_tags(client):
    svc = PublishingService(client=client)
    item = {"id": 42, "fields": {"System.Tags": "needs-refinement; repo:alpha; extra"}}
    svc.publish(item, _findings_done(), allow_title_edits=False,
                tag_trigger="needs-refinement", tag_done="refinement-done",
                tag_blocked="refinement-blocked",
                result_markdown=None, attachment_name=None)
    client.patch_description.assert_not_called()
    client.patch_acceptance_criteria.assert_not_called()
    client.comment.assert_called_once()
    client.patch_title.assert_not_called()  # title edits disabled
    client.remove_tag.assert_called_once_with(item, "needs-refinement")
    client.add_tag.assert_called_once_with(item, "refinement-done")

def test_publish_does_not_transition_when_comment_fails(client):
    client.comment.side_effect = RuntimeError("comment unavailable")
    svc = PublishingService(client=client)
    item = {"id": 42, "fields": {"System.Tags": "needs-refinement; repo:alpha"}}
    with pytest.raises(RuntimeError, match="comment unavailable"):
        svc.publish(item, _findings_done(), allow_title_edits=False,
                    tag_trigger="needs-refinement", tag_done="refinement-done",
                    tag_blocked="refinement-blocked",
                    result_markdown=None, attachment_name=None)
    client.add_tag.assert_not_called()
    client.remove_tag.assert_not_called()


def test_publish_done_patches_title_when_allowed_and_provided(client):
    svc = PublishingService(client=client)
    item = {"id": 1, "fields": {"System.Tags": "needs-refinement"}}
    svc.publish(item, _findings_done(), allow_title_edits=True,
                tag_trigger="needs-refinement", tag_done="refinement-done",
                tag_blocked="refinement-blocked",
                result_markdown=None, attachment_name=None)
    client.patch_title.assert_called_once_with(1, "Better T")


def test_publish_uploads_attachment_when_provided(client):
    svc = PublishingService(client=client)
    item = {"id": 5, "fields": {}}
    captured = {}
    svc.publish(
        item, _findings_done(),
        allow_title_edits=False,
        tag_trigger="t", tag_done="d", tag_blocked="b",
        result_markdown="# markdown",
        attachment_name="a.md",
        on_attachment_seconds=lambda s: captured.setdefault("secs", []).append(s),
    )
    client.upload_attachment.assert_called_once_with("a.md", b"# markdown")
    client.add_attachment_relation.assert_called_once_with(5, "https://example/att")
    assert "secs" in captured


def test_publish_skips_attachment_when_markdown_missing(client):
    svc = PublishingService(client=client)
    item = {"id": 6, "fields": {}}
    svc.publish(item, _findings_done(), allow_title_edits=False,
                tag_trigger="t", tag_done="d", tag_blocked="b",
                result_markdown=None, attachment_name=None)
    client.upload_attachment.assert_not_called()


def test_build_result_markdown_contains_sections():
    md = build_result_markdown(
        {"id": 1, "fields": {"System.Title": "T"}},
        _findings_done(),
    )
    for header in ("# Refinement result for #1", "## Title", "## Facts",
                   "## Classes", "## API specs"):
        assert header in md


def test_versioned_attachment_name_includes_id_and_title():
    item = {"id": 99, "fields": {"System.Title": "Fix Login Bug"}}
    name = versioned_attachment_name(item)
    assert name.startswith("refinement-99-")
    assert name.endswith("-Fix-Login-Bug.md")


def test_versioned_attachment_name_falls_back_when_title_blank():
    item = {"id": 1, "fields": {"System.Title": ""}}
    name = versioned_attachment_name(item)
    assert name.startswith("refinement-1-")
    assert "-item.md" in name


# --- module-level pure helpers (kept exposed for backwards-compat) ---


def test_findings_to_html_escapes_special_chars():
    from services.publishing_service import findings_to_html
    out = findings_to_html({"facts": ["<script>alert(1)</script>"], "classes": [], "api_specs": []})
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_findings_to_ac_html_is_stub():
    from services.publishing_service import findings_to_ac_html
    out = findings_to_ac_html({"classes": []})
    assert "<!--" in out


def test_format_unknowns_lists_questions():
    from services.publishing_service import format_unknowns
    out = format_unknowns({"unknowns": [{"question": "Q1", "why": "W1"}, {"question": "Q2", "why": "W2"}]})
    assert "Q1" in out and "W1" in out and "Q2" in out


def test_format_summary_counts():
    from services.publishing_service import format_summary
    out = format_summary({"facts": ["a", "b"], "classes": [{}], "api_specs": [], "sourceRefs": ["x", "y"]})
    assert "Facts: 2" in out
    assert "Classes: 1" in out
    assert "Source refs: 2" in out
