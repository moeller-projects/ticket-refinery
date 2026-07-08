"""ado_client: pure-block helper + REST methods hit via requests mock."""
from unittest.mock import MagicMock, patch

import pytest
import requests

import ado_client


# --- pure: _replace_block -------------------------------------------------

def test_replace_block_appends_when_empty():
    out = ado_client._replace_block("", "<p>hi</p>")
    assert out.startswith("<!-- ticket-refinery:begin -->")
    assert "<p>hi</p>" in out
    assert out.rstrip().endswith("<!-- ticket-refinery:end -->")


def test_replace_block_appends_to_existing_html():
    out = ado_client._replace_block("<p>existing</p>", "<p>new</p>")
    assert "<p>existing</p>" in out
    assert "<p>new</p>" in out
    # New block appended after existing, not inlined.
    assert out.index("<p>existing</p>") < out.index("<p>new</p>")


def test_replace_block_replaces_existing_block_idempotently():
    original = "<p>x</p>\n<!-- ticket-refinery:begin -->\nOLD\n<!-- ticket-refinery:end -->\n<p>y</p>"
    out = ado_client._replace_block(original, "NEW")
    assert "OLD" not in out
    assert out.count("<!-- ticket-refinery:begin -->") == 1
    assert "NEW" in out


def test_replace_block_ignores_stray_marker_without_pair():
    # begin without end (or out of order) → don't try to splice, just append.
    half = "<p>x</p>\n<!-- ticket-refinery:begin -->\nNEW"
    out = ado_client._replace_block(half, "<p>diff</p>")
    # Both the orphan and the new block should be present, no splicing attempted.
    assert "<!-- ticket-refinery:begin -->" in out
    assert "diff" in out


# --- REST methods ----------------------------------------------------------

def _patch_response(status=200, json_data=None, text="", content_type="application/json"):
    r = MagicMock()
    r.status_code = status
    r.headers = {"content-type": content_type}
    r.json.return_value = json_data or {}
    r.text = text
    r.raise_for_status = MagicMock()
    return r


def test_query_items_uses_wiql_and_fetches_batch(monkeypatch):
    c = ado_client.AdoClient("org", "proj", "pat")
    wiql_resp = _patch_response(json_data={"workItems": [{"id": 1}, {"id": 2}]})
    items_resp = _patch_response(json_data={"value": [{"id": 1, "fields": {}}, {"id": 2, "fields": {}}]})
    posts, gets = [], []

    def fake_post(url, json=None, **kw):
        posts.append((url, json))
        return wiql_resp

    def fake_get(url, **kw):
        gets.append(url)
        return items_resp

    with patch.object(ado_client.requests, "post", side_effect=fake_post), \
         patch.object(ado_client.requests, "get", side_effect=fake_get):
        items = c.query_items("needs-refinement", ["refinement-done", "refinement-blocked"])

    assert len(items) == 2
    assert len(posts) == 1
    wiql_url, body = posts[0]
    assert wiql_url.endswith(f"/_apis/wit/wiql?api-version={ado_client.API_VERSION}")
    assert "[System.Tags] CONTAINS 'needs-refinement'" in body["query"]
    assert "[System.Tags] NOT CONTAINS 'refinement-done'" in body["query"]
    assert "[System.Tags] NOT CONTAINS 'refinement-blocked'" in body["query"]
    assert len(gets) == 1 and "ids=1,2" in gets[0]


def test_query_items_returns_empty_when_no_workitems(monkeypatch):
    c = ado_client.AdoClient("org", "proj", "pat")
    wiql_resp = _patch_response(json_data={"workItems": []})
    with patch.object(ado_client.requests, "post", return_value=wiql_resp) as post:
        items = c.query_items("t", [])
    assert items == []
    # No GET batch call when there are no ids.
    assert post.call_count == 1


def test_query_items_surfaces_non_json_response(monkeypatch, tmp_path):
    # Auth challenge returns HTML with 2xx; URL+status+body must surface, body to file.
    monkeypatch.setattr(ado_client, "NON_JSON_BODY_DUMP", tmp_path / "dump.html")
    c = ado_client.AdoClient("org", "proj", "pat")
    body = "<html>login</html>" + ("x" * 2000)  # > 500 chars on purpose
    bad = _patch_response(text=body, content_type="text/html")
    with patch.object(ado_client.requests, "post", return_value=bad):
        with pytest.raises(requests.exceptions.HTTPError) as ei:
            c.query_items("t", [])
    msg = str(ei.value)
    assert "non-JSON" in msg
    assert "status 200" in msg
    assert "text/html" in msg
    assert "/org/proj/" in msg
    # Full body — not truncated — is dumped to disk.
    assert (tmp_path / "dump.html").read_text() == body


def test_query_items_logs_full_body_when_non_json(monkeypatch, tmp_path, capfd):
    monkeypatch.setattr(ado_client, "NON_JSON_BODY_DUMP", tmp_path / "dump.html")
    c = ado_client.AdoClient("org", "proj", "pat")
    body = "<html>login</html>" + ("z" * 2000)
    bad = _patch_response(text=body, content_type="text/html")
    with patch.object(ado_client.requests, "post", return_value=bad):
        with pytest.raises(requests.exceptions.HTTPError):
            c.query_items("t", [])
    captured = capfd.readouterr().err
    assert "--- body begin ---" in captured
    assert body in captured
    assert "--- body end ---" in captured


def test_query_items_raises_for_4xx(monkeypatch):
    c = ado_client.AdoClient("org", "proj", "pat")
    bad = _patch_response(status=401, text="unauthorized", content_type="text/plain")
    with patch.object(ado_client.requests, "post", return_value=bad):
        with pytest.raises(requests.exceptions.HTTPError, match="HTTP 401"):
            c.query_items("t", [])


def test_add_tag_is_idempotent(monkeypatch):
    c = ado_client.AdoClient("org", "proj", "pat")
    item = {"id": 7, "fields": {"System.Tags": "needs-refinement; existing"}}
    with patch.object(c, "_patch") as p:
        # Already present → no patch issued.
        c.add_tag(item, "needs-refinement")
        c.add_tag(item, "existing")
        p.assert_not_called()
        # Truly new → one patch with appended semicolon-list.
        c.add_tag(item, "refinement-blocked")
        p.assert_called_once()
        item_id, field, value = p.call_args.args
        assert item_id == 7 and field == "System.Tags"
        assert value == "needs-refinement; existing; refinement-blocked"


def test_remove_tag_drops_target_and_preserves_others(monkeypatch):
    c = ado_client.AdoClient("org", "proj", "pat")
    item = {"id": 9, "fields": {"System.Tags": "a; b; c"}}
    with patch.object(c, "_patch") as p:
        c.remove_tag(item, "b")
        p.assert_called_once()
        _, _, value = p.call_args.args
        assert value == "a; c"


def test_remove_tag_no_op_when_absent(monkeypatch):
    c = ado_client.AdoClient("org", "proj", "pat")
    item = {"id": 9, "fields": {"System.Tags": "a"}}
    # Still calls _patch (idempotent write to "" tags is cheap), but value lacks target.
    with patch.object(c, "_patch") as p:
        c.remove_tag(item, "missing")
        p.assert_called_once()
        _, _, value = p.call_args.args
        assert value == "a"


def test_patch_description_wraps_existing_in_block(monkeypatch):
    c = ado_client.AdoClient("org", "proj", "pat")
    item = {"id": 11, "fields": {"System.Description": "<p>orig</p>"}}
    with patch.object(c, "_patch") as p:
        c.patch_description(item, "<p>new</p>")
        p.assert_called_once()
        item_id, field, value = p.call_args.args
        assert item_id == 11 and field == "System.Description"
        assert "<p>orig</p>" in value and "<p>new</p>" in value
        assert value.count("<!-- ticket-refinery:begin -->") == 1


def test_patch_acceptance_criteria_field_name(monkeypatch):
    c = ado_client.AdoClient("org", "proj", "pat")
    item = {"id": 11, "fields": {"Microsoft.VSTS.Common.AcceptanceCriteria": "<p>orig</p>"}}
    with patch.object(c, "_patch") as p:
        c.patch_acceptance_criteria(item, "<p>new</p>")
        _, field, _ = p.call_args.args
        assert field == "Microsoft.VSTS.Common.AcceptanceCriteria"


def test_comment_posts_to_comments_api(monkeypatch):
    c = ado_client.AdoClient("org", "proj", "pat")
    resp = _patch_response()
    with patch.object(ado_client.requests, "post", return_value=resp) as post:
        c.comment(11, "hello")
        url, kwargs = post.call_args.args, post.call_args.kwargs
        assert "/_apis/wit/workitems/11/comments" in url[0]
        assert kwargs["json"] == {"text": "hello"}


def test_make_auth_builds_bearer_header():
    headers = ado_client._make_auth("secret")
    assert headers == {"Authorization": "Bearer secret"}


def test_make_auth_raises_when_nothing_configured(monkeypatch):
    with pytest.raises(RuntimeError, match="No ADO credentials"):
        ado_client._make_auth(None)
