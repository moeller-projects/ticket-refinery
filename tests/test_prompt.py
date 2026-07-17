"""prompt template: curated RepositoryContext placeholders present, schema left intact."""
import json
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
PROMPT_PATH = SRC / "prompts" / "refine.prompt.tmpl.md"
SCHEMA_PATH = SRC / "schema" / "findings.schema.json"


def test_prompt_contains_curated_repo_context_placeholder():
    """The prompt receives a pre-rendered RepositoryContext markdown block."""
    text = PROMPT_PATH.read_text()
    assert "{repo_context}" in text


def test_prompt_no_longer_mentions_codegraph_tools():
    """Repository intelligence is gathered before the prompt is built; Pi
    must not be told to invoke any codegraph_* tools itself."""
    text = PROMPT_PATH.read_text().lower()
    for forbidden in (
        "codegraph_search", "codegraph_callers", "codegraph_callees",
        "codegraph_impact", "codegraph_explore", "codegraph_node",
        "codegraph_*",
    ):
        assert forbidden not in text, f"stale codegraph tool reference: {forbidden}"


def test_prompt_explains_repo_context_handling():
    text = PROMPT_PATH.read_text().lower()
    assert "curated" in text
    assert "reason over" in text
    # Either the explicit "no repository-wide scan" wording or the
    # "do not re-discover the repository yourself with grep/find/ls"
    # framing — both convey the same intent.
    assert ("no repository-wide" in text
            or "do not re-discover" in text)
    assert "degraded" in text or "graph not ready" in text

def test_prompt_requires_blocking_unknowns_and_minimal_technical_evidence():
    text = PROMPT_PATH.read_text().lower()
    assert "only when the uncertainty blocks implementation" in text
    assert "do not dump or reproduce complete files" in text
    assert "objects/classes" in text
    assert "api endpoints" in text
    assert "short exact code snippet" in text
    assert "concise pseudocode" in text
    assert "without inventing repository citations" in text
    assert "never infer them from naming conventions" in text
    assert "maximum 20 `facts`" in text


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


def test_rendered_prompt_substitutes_repo_context(tmp_path, monkeypatch):
    """When the renderer receives a repo_context_section, the {repo_context}
    placeholder is replaced with that markdown."""
    import refine
    monkeypatch.setattr(refine, "SCHEMA", tmp_path / "s.json")
    monkeypatch.setattr(refine, "PROMPT", tmp_path / "p.md")
    (tmp_path / "s.json").write_text("")
    (tmp_path / "p.md").write_text("CTX={repo_context}|X")
    item = {"fields": {"System.Title": "T"}}
    out = refine.render_prompt(item, [], Path("/w"), comments_text="",
                               repo_context_section="CURATED-CONTENT")
    assert "CTX=CURATED-CONTENT|X" in out
    # Default (no context): placeholder becomes empty.
    out2 = refine.render_prompt(item, [], Path("/w"), comments_text="")
    assert "CTX=|X" in out2


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
        "{repo_context}",
    ):
        assert placeholder in text, f"missing placeholder: {placeholder}"


def test_prompt_does_not_use_markdown_fence_around_schema():
    text = PROMPT_PATH.read_text()
    # Schema is injected raw — no ```json fences around it in the template.
    bad = "```json\n" + json.dumps(json.loads(SCHEMA_PATH.read_text()))[:20]
    assert bad not in text