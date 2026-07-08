"""prompt template: CodeGraph-first instructions present, schema left intact."""
import json
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
PROMPT_PATH = SRC / "prompts" / "refine.prompt.tmpl.md"
SCHEMA_PATH = SRC / "schema" / "findings.schema.json"


def test_prompt_instructs_codegraph_first():
    text = PROMPT_PATH.read_text()
    assert "CodeGraph" in text
    assert "codegraph" in text.lower()
    assert "structural" in text.lower()
    # CodeGraph must come BEFORE filesystem tools in priority.
    cg_pos = text.lower().find("codegraph")
    fs_pos = text.lower().find("filesystem")
    assert cg_pos < fs_pos, "codegraph must be listed before filesystem tools"


def test_prompt_lists_specific_codegraph_tools():
    text = PROMPT_PATH.read_text().lower()
    for tool in ("codegraph_search", "codegraph_callers", "codegraph_callees",
                 "codegraph_impact", "codegraph_explore", "codegraph_node"):
        assert tool in text, f"missing tool reference: {tool}"


def test_prompt_warns_against_repeated_filesystem_traversal():
    text = PROMPT_PATH.read_text().lower()
    assert "never re-traverse" in text or "not re-traverse" in text
    assert "fallback" in text


def test_prompt_has_schema_placeholder():
    """{schema} placeholder is present; the actual schema is injected at
    render time by refine.render_prompt (keeping the prompt template clean
    of the volatile JSON content).
    """
    prompt_text = PROMPT_PATH.read_text()
    assert '{schema}' in prompt_text


def test_rendered_prompt_carries_live_schema_text():
    # End-to-end: built prompt should embed the actual schema as-is.
    import refine
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td) / "ws"
        ws.mkdir()
        # Keep module-level paths; render with default. Use a dummy item.
        item = {"fields": {"System.Title": "T"}}
        text = refine.render_prompt(item, ["r"], ws, comments_text="")
        # Schema content is appended after substitution.
        assert '"$schema"' in text
        assert '"required"' in text
        assert "facts" in text
        assert "sourceRefs" in text


def test_prompt_includes_input_placeholders():
    text = PROMPT_PATH.read_text()
    for placeholder in (
        "{title}",
        "{description}",
        "{acceptance_criteria}",
        "{repo_list}",
        "{comments}",
        "{schema}",
        "{workspace}",
        "{target_language}",
    ):
        assert placeholder in text, f"missing placeholder: {placeholder}"


def test_prompt_does_not_use_markdown_fence_around_schema():
    text = PROMPT_PATH.read_text()
    # Schema is injected raw — no ```json fences around it in the template.
    bad = "```json\n" + json.dumps(json.loads(SCHEMA_PATH.read_text()))[:20]
    assert bad not in text
