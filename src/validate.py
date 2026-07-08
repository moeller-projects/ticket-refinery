"""Findings JSON: schema check + sourceRef existence check."""
import json
from pathlib import Path

from jsonschema import Draft7Validator, validate
from jsonschema.exceptions import ValidationError


def check(findings: dict, workspace: Path, schema_path: Path, known_repos: list[str] | None = None) -> None:
    """Raise ValidationError if findings don't match schema or sourceRefs don't resolve."""
    schema = json.loads(schema_path.read_text())
    Draft7Validator.check_schema(schema)
    validate(instance=findings, schema=schema)
    _normalize_and_check_source_refs(findings, workspace, known_repos or [])


def _normalize_and_check_source_refs(findings: dict, workspace: Path, known_repos: list[str]) -> None:
    _normalize_source_ref_containers(findings)
    _check_source_refs(findings, workspace, known_repos)


def _normalize_source_ref_containers(findings: dict) -> None:
    for key in ("sourceRefs",):
        findings[key] = _split_refs(findings.get(key, []))
    for section in ("dtos", "api_specs"):
        for obj in findings.get(section, []):
            if "sourceRef" in obj:
                refs = _split_refs([obj["sourceRef"]])
                obj["sourceRef"] = refs[0] if refs else obj["sourceRef"]


def _split_refs(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        for part in str(value).split(";"):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _check_source_refs(findings: dict, workspace: Path, known_repos: list[str]) -> None:
    bad = []
    for ref in findings.get("sourceRefs", []):
        if not _ref_resolves(ref, workspace, known_repos):
            bad.append(ref)
    for section in ("dtos", "api_specs"):
        for obj in findings.get(section, []):
            r = obj.get("sourceRef")
            if r and not _ref_resolves(r, workspace, known_repos):
                bad.append(r)
    if bad:
        raise ValidationError(f"Unresolved sourceRefs: {bad}")


def _ref_resolves(ref: str, workspace: Path, known_repos: list[str] | None = None) -> bool:
    # format: <repo-name>[/|:]<relative-path>#L<line[-Lend]>
    # Both separators accepted: prompt canonicalises on `:` but the LLM often
    # emits `repo/path/file.ext` (filesystem-natural). Permissive at the boundary.
    path_part = ref.split("#", 1)[0]
    if ":" in path_part:
        repo, rel = path_part.split(":", 1)
    elif "/" in path_part:
        repo, _, rel = path_part.partition("/")
    else:
        return False
    known_repos = known_repos or []
    if (workspace / repo / rel).exists():
        return True
    # ponytail: bare paths like `modules/orders/...` reach us without the
    # `<repo>:` prefix the prompt asks for. Fall back to looking under each
    # known repo dir so single-repo work items don't false-negative.
    if ":" not in path_part and repo == path_part.split("/", 1)[0]:
        for kr in known_repos:
            if (workspace / kr / path_part).exists():
                return True
    return False


if __name__ == "__main__":  # ponytail: one-shot self-check
    import tempfile, textwrap
    schema = json.loads(textwrap.dedent("""
        {"type":"object","required":["facts","dtos","api_specs","unknowns","sourceRefs"],
         "properties":{"facts":{"type":"array","items":{"type":"string"}},
                       "dtos":{"type":"array","items":{"type":"object"}},
                       "api_specs":{"type":"array","items":{"type":"object"}},
                       "unknowns":{"type":"array","items":{"type":"object"}},
                       "sourceRefs":{"type":"array","items":{"type":"string"}}}}
    """).strip())
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "r").mkdir()
        (ws / "r" / "f.py").write_text("x = 1\n")
        schema_file = Path(td) / "s.json"
        schema_file.write_text(json.dumps(schema))
        findings = {"facts": [], "dtos": [], "api_specs": [],
                    "unknowns": [], "sourceRefs": ["r:f.py#L1"]}
        check(findings, ws, schema_file)  # must not raise
        findings["sourceRefs"].append("r:nope.py#L1")
        try:
            check(findings, ws, schema_file)
        except ValidationError:
            print("validate ok: bad ref rejected")