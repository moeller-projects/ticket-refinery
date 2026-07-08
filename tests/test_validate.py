"""validate.check: schema + every sourceRef resolves to a real file."""
import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

import validate


@pytest.fixture
def schema(tmp_path):
    s = {
        "type": "object",
        "required": ["facts", "dtos", "api_specs", "unknowns", "sourceRefs"],
        "properties": {
            "facts": {"type": "array", "items": {"type": "string"}},
            "dtos": {"type": "array", "items": {"type": "object"}},
            "api_specs": {"type": "array", "items": {"type": "object"}},
            "unknowns": {"type": "array", "items": {"type": "object"}},
            "sourceRefs": {"type": "array", "items": {"type": "string"}},
        },
    }
    p = tmp_path / "s.json"
    p.write_text(json.dumps(s))
    return p


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "f.py").write_text("x = 1\n")
    return tmp_path


def _base_findings() -> dict:
    return {"facts": [], "dtos": [], "api_specs": [], "unknowns": [], "sourceRefs": []}


def test_check_accepts_minimal_valid_payload(ws, schema):
    findings = _base_findings()
    findings["sourceRefs"] = ["r:f.py#L1"]
    validate.check(findings, ws, schema)  # must not raise


def test_check_splits_semicolon_joined_refs(ws, schema):
    findings = _base_findings()
    findings["sourceRefs"] = ["r:f.py#L1; r:f.py#L1"]
    validate.check(findings, ws, schema)
    assert findings["sourceRefs"] == ["r:f.py#L1", "r:f.py#L1"]


def test_check_rejects_missing_required_field(ws, schema):
    findings = {"facts": [], "dtos": []}  # missing 3 required
    with pytest.raises(ValidationError):
        validate.check(findings, ws, schema)


def test_check_rejects_unresolved_top_level_ref(ws, schema):
    findings = _base_findings()
    findings["sourceRefs"] = ["r:does_not_exist.py#L1"]
    with pytest.raises(ValidationError, match="Unresolved sourceRefs"):
        validate.check(findings, ws, schema)


def test_check_rejects_unresolved_dto_ref(ws, schema):
    findings = _base_findings()
    findings["dtos"] = [{"name": "Foo", "fields": [], "sourceRef": "r:nope.py#L1"}]
    with pytest.raises(ValidationError, match="Unresolved sourceRefs"):
        validate.check(findings, ws, schema)


def test_check_rejects_unresolved_api_ref(ws, schema):
    findings = _base_findings()
    findings["api_specs"] = [
        {"method": "GET", "path": "/x", "sourceRef": "r:nope.py#L1"},
    ]
    with pytest.raises(ValidationError, match="Unresolved sourceRefs"):
        validate.check(findings, ws, schema)


def test_ref_resolves_requires_separator(ws):
    from validate import _ref_resolves
    assert _ref_resolves("noColon", ws) is False  # malformed: no separator
    assert _ref_resolves("r:does_not_exist.py", ws) is False  # colon form, missing file
    assert _ref_resolves("r/f.py", ws) is True  # slash form accepted too
    assert _ref_resolves("r/nope.py", ws) is False  # slash form, missing file
    assert _ref_resolves("r:f.py", ws) is True


def test_check_raises_when_schema_file_missing(ws):
    # Schema file must be readable JSON; missing file → JSONDecodeError chain.
    findings = _base_findings()
    with pytest.raises(FileNotFoundError):
        validate.check(findings, ws, ws / "nope.json")
