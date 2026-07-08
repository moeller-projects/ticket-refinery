"""refine.py: env parsing, JSONC, tag/repo extraction, prompt rendering,
   findings formatting, Config, and process_item (all externals mocked)."""
import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import refine
import pi_runner
import ado_client


# --- _clean ---------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ('"hello"', "hello"),                 # ASCII double
    ("'hello'", "hello"),                 # ASCII single
    ('"hello" # trailing', "hello"),      # inline comment stripped
    ("\"hello\"\r", "hello"),             # CRLF
    ('\u201chello\u201d', "hello"),       # smart double
    ('\u2018hello\u2019', "hello"),       # smart single
    ("hello", "hello"),                   # bare value
    ("  hello  ", "hello"),               # outer whitespace
    ('"#abc"', "#abc"),                   # PAT-style: quoted # preserved
    ("#abc", "#abc"),                     # inline # without leading ws preserved (PAT ceiling)
])
def test_clean(raw, expected):
    assert refine._clean(raw) == expected


def test_clean_quotes_only_match_one_round():
    # Mismatched pair "foo' strips once; inner unmatched doesn't trigger another round.
    assert refine._clean('"hello') == '"hello'
    assert refine._clean("hello'") == "hello'"


# --- _load_jsonc ----------------------------------------------------------

def test_load_jsonc_strips_line_comments(tmp_path):
    f = tmp_path / "x.jsonc"
    f.write_text('// header\n{"a": 1}\n')
    assert refine._load_jsonc(f) == {"a": 1}


def test_load_jsonc_strips_block_comments(tmp_path):
    f = tmp_path / "x.jsonc"
    f.write_text('/* hi */{"a": /* inner */ 1}')
    assert refine._load_jsonc(f) == {"a": 1}


def test_load_jsonc_strips_trailing_commas(tmp_path):
    f = tmp_path / "x.jsonc"
    f.write_text('{"a": 1, "b": 2,}')
    assert refine._load_jsonc(f) == {"a": 1, "b": 2}


def test_load_jsonc_preserves_https_slash_slash_in_values(tmp_path):
    f = tmp_path / "x.jsonc"
    f.write_text('{"url": "https://example.com/x"}')
    assert refine._load_jsonc(f) == {"url": "https://example.com/x"}


def test_load_jsonc_real_repos_cfg():
    # The actual repos.jsonc ships with comments + trailing commas; it must parse.
    data = refine._load_jsonc(refine.REPOS_CFG)
    assert "Laekkerai.Ordering" in data
    assert data["Laekkerai.Ordering"]["defaultBranch"] == "dev"


# --- extract_repo_tags + resolve_repos -----------------------------------

def test_extract_repo_tags_empty():
    assert refine.extract_repo_tags({"fields": {"System.Tags": ""}}) == []
    assert refine.extract_repo_tags({"fields": {}}) == []


def test_extract_repo_tags_filters_and_preserves_order():
    item = {"fields": {"System.Tags": "repo:alpha; needs-refinement; repo:beta"}}
    assert refine.extract_repo_tags(item) == ["alpha", "beta"]


def test_extract_repo_tags_namespaced_after_first_colon():
    item = {"fields": {"System.Tags": "repo:ns.sub"}}
    assert refine.extract_repo_tags(item) == ["ns.sub"]


def test_resolve_repos_returns_named_dicts():
    repo_map = {"alpha": {"url": "u-a", "defaultBranch": "main"},
                "beta": {"url": "u-b", "defaultBranch": "main"}}
    out = refine.resolve_repos(["beta", "alpha"], repo_map)
    assert [r["name"] for r in out] == ["beta", "alpha"]
    assert out[0]["url"] == "u-b"


def test_resolve_repos_raises_on_unknown():
    repo_map = {"alpha": {"url": "u-a", "defaultBranch": "main"}}
    with pytest.raises(pi_runner.InfraError, match="not in repos.jsonc"):
        refine.resolve_repos(["alpha", "ghost"], repo_map)


def test_resolve_repos_empty():
    assert refine.resolve_repos([], {}) == []


# --- render_prompt --------------------------------------------------------

def test_render_prompt_substitutes_placeholders(tmp_path, monkeypatch):
    # SCHEMA + PROMPT paths are module-level; point at temp files we control.
    monkeypatch.setattr(refine, "SCHEMA", tmp_path / "s.json")
    monkeypatch.setattr(refine, "PROMPT", tmp_path / "p.md")
    (tmp_path / "s.json").write_text('{"$schema":"..."}')
    (tmp_path / "p.md").write_text(
        "WS={workspace} TITLE={title} DESC={description} "
        "AC={acceptance_criteria} REPOS={repo_list} SCHEMA={schema}"
    )
    item = {"fields": {
        "System.Title": "T",
        "System.Description": "D",
        "Microsoft.VSTS.Common.AcceptanceCriteria": "AC",
    }}
    out = refine.render_prompt(item, ["a", "b"], Path("/w"))
    assert "WS=/w" in out and "TITLE=T" in out and "DESC=D" in out
    assert "AC=AC" in out and "REPOS=a, b" in out
    assert "SCHEMA={\"$schema\":\"...\"}" in out


def test_render_prompt_handles_missing_description_and_ac(tmp_path, monkeypatch):
    monkeypatch.setattr(refine, "SCHEMA", tmp_path / "s.json")
    monkeypatch.setattr(refine, "PROMPT", tmp_path / "p.md")
    (tmp_path / "s.json").write_text("")
    (tmp_path / "p.md").write_text("D={description}|AC={acceptance_criteria}")
    item = {"fields": {"System.Title": "T"}}  # no desc/ac keys
    out = refine.render_prompt(item, [], Path("/w"))
    assert "D=|AC=" in out  # empty defaults


# --- findings_to_html / format_* -----------------------------------------

def test_findings_to_html_includes_sections_and_escapes():
    findings = {
        "facts": ["plain", "<script>alert(1)</script>"],
        "dtos": [{"name": "<X>", "sourceRef": "r:f.py#L1", "fields": []}],
        "api_specs": [{"method": "GET", "path": "/v", "sourceRef": "r:f.py#L2"}],
    }
    out = refine.findings_to_html(findings)
    assert "### Facts" in out
    assert "### DTOs" in out
    assert "### API specs" in out
    # Raw <script> must be escaped, not embedded as live HTML.
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&lt;X&gt;" in out  # DTO name escaped


def test_findings_to_html_empty_payload():
    out = refine.findings_to_html({})
    assert out == "### Facts"


def test_findings_to_ac_html_is_stub_string():
    assert "auto-derived" in refine.findings_to_ac_html({})


def test_format_unknowns_lists_questions():
    findings = {"unknowns": [{"question": "Q1?", "why": "no code"}, {"question": "Q2?", "why": "spec unclear"}]}
    out = refine.format_unknowns(findings)
    assert "Refinement blocked" in out
    assert "- **Q1?** — no code" in out
    assert "- **Q2?** — spec unclear" in out


def test_format_summary_counts_sections():
    findings = {"facts": [1, 2], "dtos": [{}], "api_specs": [], "sourceRefs": ["r:f.py#L1"]}
    out = refine.format_summary(findings)
    assert "Facts: 2" in out and "DTOs: 1" in out and "API specs: 0" in out
    assert "Source refs: 1" in out


# --- Config.from_env ------------------------------------------------------

def _base_env():
    return {
        "ADO_ORG": "org", "ADO_PROJECT": "proj",
        "TAG_TRIGGER": "needs-refinement", "TAG_DONE": "refinement-done",
        "TAG_BLOCKED": "refinement-blocked",
        "CLONE_DEPTH": "1",
        "PI_MODEL": "model-x",
    }


def _with_pat(env):
    env = dict(env)
    env["ADO_PAT"] = "pat"
    return env


def test_config_from_env_happy(monkeypatch):
    for k, v in _with_pat(_base_env()).items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ALLOW_TITLE_EDITS", raising=False)
    cfg = refine.Config.from_env()
    assert cfg.ado_org == "org" and cfg.clone_depth == 1
    assert cfg.allow_title_edits is False


def test_config_from_env_quoted_values(monkeypatch):
    env = _base_env()
    env["ADO_ORG"] = '"my-org"'   # podman-quoted
    env["CLONE_DEPTH"] = "'1'"
    env["ADO_PAT"] = "pat"
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = refine.Config.from_env()
    assert cfg.ado_org == "my-org" and cfg.clone_depth == 1


def test_config_from_env_missing_required_exits(monkeypatch):
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ADO_PAT", raising=False)
    with pytest.raises(SystemExit, match="Missing ADO credentials"):
        refine.Config.from_env()


def test_config_from_env_allow_title_edits_parses(monkeypatch):
    env = _base_env()
    env["ALLOW_TITLE_EDITS"] = "TRUE"
    env["ADO_PAT"] = "pat"
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = refine.Config.from_env()
    assert cfg.allow_title_edits is True


# --- process_item (integration of refine, with all externals mocked) ------

def _make_config(**overrides):
    base = dict(
        ado_org="org", ado_project="proj", ado_pat="pat",
        tag_trigger="needs-refinement", tag_done="refinement-done",
        tag_blocked="refinement-blocked", allow_title_edits=False,
        clone_depth=1, pi_model="m",
    )
    base.update(overrides)
    return refine.Config(**base)


def _patch_git_ops(monkeypatch):
    monkeypatch.setattr(refine.git_ops, "clone_all", lambda *a, **kw: None)
    monkeypatch.setattr(refine.git_ops, "cleanup", lambda *a, **kw: None)



def _make_client():
    c = MagicMock()
    c.comment = MagicMock()
    c.add_tag = MagicMock()
    c.remove_tag = MagicMock()
    c.patch_description = MagicMock()
    c.patch_acceptance_criteria = MagicMock()
    c.patch_title = MagicMock()
    return c


def test_process_item_happy_path_writes_back_and_transitions(monkeypatch):
    _patch_git_ops(monkeypatch)

    item = {
        "id": 42,
        "fields": {
            "System.Tags": "repo:alpha; needs-refinement",
            "System.Title": "T",
            "System.Description": "D",
            "Microsoft.VSTS.Common.AcceptanceCriteria": "",
        },
    }
    repos_map = {"alpha": {"url": "u-a", "defaultBranch": "main"}}
    client = _make_client()
    cfg = _make_config()

    monkeypatch.setattr(refine, "render_prompt",
                        lambda *a, **kw: "PROMPT")
    findings = {
        "facts": ["f1"], "dtos": [{"name": "D", "sourceRef": "alpha:f.py#L1", "fields": []}],
        "api_specs": [], "unknowns": [], "sourceRefs": ["alpha:f.py#L1"],
        "suggested_title": None,
    }
    monkeypatch.setattr(refine.pi_runner, "run", lambda *a, **kw: findings)
    monkeypatch.setattr(refine.validate, "check", lambda *a, **kw: None)

    refine.process_item(item, cfg, client, repos_map, logging.getLogger("t"))

    client.patch_description.assert_called_once()
    client.patch_acceptance_criteria.assert_called_once()
    client.comment.assert_called_once()  # summary, no unknowns
    client.add_tag.assert_called_with(item, cfg.tag_done)
    client.remove_tag.assert_called_with(item, cfg.tag_trigger)
    client.patch_title.assert_not_called()


def test_process_item_reuses_cached_repo_via_symlink(tmp_path):
    cache = tmp_path / "cache"
    (cache / "alpha").mkdir(parents=True)
    workspace = tmp_path / "w"
    refine._link_repo_cache([{"name": "alpha"}], cache, workspace)
    assert (workspace / "alpha").is_symlink()
    assert (workspace / "alpha").resolve() == cache / "alpha"



def test_process_item_unknowns_takes_blocked_branch(monkeypatch):
    _patch_git_ops(monkeypatch)

    item = {"id": 7, "fields": {"System.Tags": "repo:alpha"}}
    repos_map = {"alpha": {"url": "u-a", "defaultBranch": "main"}}
    client = _make_client()
    cfg = _make_config()

    monkeypatch.setattr(refine, "render_prompt", lambda *a, **kw: "P")
    findings = {
        "facts": [], "dtos": [], "api_specs": [],
        "unknowns": [{"question": "?", "why": "w"}], "sourceRefs": [],
    }
    monkeypatch.setattr(refine.pi_runner, "run", lambda *a, **kw: findings)
    monkeypatch.setattr(refine.validate, "check", lambda *a, **kw: None)

    refine.process_item(item, cfg, client, repos_map, logging.getLogger("t"))

    client.comment.assert_called_once()
    client.add_tag.assert_called_with(item, cfg.tag_blocked)
    client.patch_description.assert_not_called()
    client.remove_tag.assert_not_called()


def test_process_item_calls_title_patch_when_allowed_and_provided(monkeypatch):
    _patch_git_ops(monkeypatch)
    item = {"id": 7, "fields": {"System.Tags": "repo:alpha"}}
    repos_map = {"alpha": {"url": "u-a", "defaultBranch": "main"}}
    client = _make_client()
    cfg = _make_config(allow_title_edits=True)

    monkeypatch.setattr(refine, "render_prompt", lambda *a, **kw: "P")
    findings = {
        "facts": [], "dtos": [], "api_specs": [],
        "unknowns": [], "sourceRefs": [],
        "suggested_title": "Better Title",
    }
    monkeypatch.setattr(refine.pi_runner, "run", lambda *a, **kw: findings)
    monkeypatch.setattr(refine.validate, "check", lambda *a, **kw: None)

    refine.process_item(item, cfg, client, repos_map, logging.getLogger("t"))

    client.patch_title.assert_called_once_with(7, "Better Title")


def test_process_item_title_patch_skipped_when_disallowed(monkeypatch):
    _patch_git_ops(monkeypatch)
    item = {"id": 7, "fields": {"System.Tags": "repo:alpha"}}
    repos_map = {"alpha": {"url": "u-a", "defaultBranch": "main"}}
    client = _make_client()
    cfg = _make_config(allow_title_edits=False)  # default

    monkeypatch.setattr(refine, "render_prompt", lambda *a, **kw: "P")
    findings = {
        "facts": [], "dtos": [], "api_specs": [],
        "unknowns": [], "sourceRefs": [],
        "suggested_title": "Should NOT PATCH",
    }
    monkeypatch.setattr(refine.pi_runner, "run", lambda *a, **kw: findings)
    monkeypatch.setattr(refine.validate, "check", lambda *a, **kw: None)

    refine.process_item(item, cfg, client, repos_map, logging.getLogger("t"))

    client.patch_title.assert_not_called()


def test_process_item_cleanup_runs_even_on_infraerror(monkeypatch):
    cleanup_called = []
    monkeypatch.setattr(refine.git_ops, "clone_all", lambda *a, **kw: None)
    monkeypatch.setattr(refine.git_ops, "cleanup", lambda ws: cleanup_called.append(ws))

    item = {"id": 7, "fields": {"System.Tags": "repo:alpha"}}
    repos_map = {"alpha": {"url": "u-a", "defaultBranch": "main"}}
    client = _make_client()
    cfg = _make_config()

    def boom(*a, **kw):
        raise pi_runner.InfraError("auth failed")
    monkeypatch.setattr(refine.pi_runner, "run", boom)

    with pytest.raises(pi_runner.InfraError):
        refine.process_item(item, cfg, client, repos_map, logging.getLogger("t"))
    assert cleanup_called  # finally branch ran
