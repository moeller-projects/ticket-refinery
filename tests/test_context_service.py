"""ContextService: comment loading + prompt rendering. AdoClient mocked."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from services.context_service import ContextService


@pytest.fixture
def schema_and_prompt(tmp_path):
    schema = tmp_path / "s.json"
    schema.write_text('{"$schema":"..."}')
    prompt = tmp_path / "p.md"
    prompt.write_text(
        "WS={workspace} TITLE={title} DESC={description} "
        "AC={acceptance_criteria} REPOS={repo_list} SCHEMA={schema} "
        "COMMENTS=[{comments}] LANG={target_language}"
    )
    return schema, prompt


def _make_client(comments=None):
    c = MagicMock()
    c.get_comments.return_value = comments or []
    return c


def test_load_comments_returns_formatted_text(schema_and_prompt):
    client = _make_client([
        {"text": "hello", "createdBy": {"displayName": "Alice"}, "createdDate": "2024-01-01"},
    ])
    svc = ContextService(client=client, schema_path=schema_and_prompt[0], prompt_path=schema_and_prompt[1])
    out = svc.load_comments(42)
    assert "Alice" in out and "hello" in out
    assert "2024-01-01" in out
    # ponytail: no surrounding brackets — the prompt template wraps the block.
    assert "[" not in out.split("\n\n")[0].split("\n")[0] or out.startswith("Alice")


def test_load_comments_calls_ado_with_top(schema_and_prompt):
    client = _make_client()
    svc = ContextService(client=client, schema_path=schema_and_prompt[0], prompt_path=schema_and_prompt[1], comment_top=5)
    svc.load_comments(7)
    client.get_comments.assert_called_once_with(7, top=5)


def test_render_prompt_replaces_placeholders(schema_and_prompt):
    svc = ContextService(client=_make_client(), schema_path=schema_and_prompt[0], prompt_path=schema_and_prompt[1])
    item = {
        "fields": {
            "System.Title": "T",
            "System.Description": "D",
            "Microsoft.VSTS.Common.AcceptanceCriteria": "AC",
        }
    }
    out = svc.render_prompt(item, ["a", "b"], Path("/w"), comments_text="(none)")
    assert "WS=/w" in out
    assert "TITLE=T" in out
    assert "DESC=D" in out
    assert "AC=AC" in out
    assert "REPOS=a, b" in out
    assert 'SCHEMA={"$schema":"..."}' in out
    assert "COMMENTS=[(none)]" in out


def test_render_prompt_handles_missing_description_and_ac(schema_and_prompt):
    svc = ContextService(client=_make_client(), schema_path=schema_and_prompt[0], prompt_path=schema_and_prompt[1])
    out = svc.render_prompt({"fields": {"System.Title": "T"}}, [], Path("/w"), "")
    # No description/AC fields → empty substitute.
    assert "DESC=" in out
    assert "AC=" in out


def test_render_prompt_uses_target_language(schema_and_prompt):
    svc = ContextService(
        client=_make_client(),
        schema_path=schema_and_prompt[0],
        prompt_path=schema_and_prompt[1],
        target_language="German",
    )
    out = svc.render_prompt({"fields": {"System.Title": "T"}}, [], Path("/w"), "")
    assert "LANG=German" in out


def test_build_inputs_loads_then_renders(schema_and_prompt):
    client = _make_client([{"text": "hi", "createdBy": {"displayName": "X"}, "createdDate": "1"}])
    svc = ContextService(client=client, schema_path=schema_and_prompt[0], prompt_path=schema_and_prompt[1])
    out = svc.build_inputs(
        item={"id": 9, "fields": {"System.Title": "T"}},
        repo_names=["a"],
        workspace=Path("/w"),
    )
    assert "COMMENTS=[X" in out and "hi]" in out
