# ticket-refinery — onboarding brief

## What it is

Containerized Python pipeline that polls Azure DevOps for work items tagged `needs-refinement`, shallow-clones the repos listed on the item into a per-item scratch workspace, asks the Pi coding agent to refine the item into structured findings, validates those findings, then writes them back to ADO (description patch, AC patch, summary comment, attachment, tag transition). One CLI command, runs in Docker, exit 0 = queue empty.

## Shape (one-line purposes)

```
src/
  refine.py                          # thin orchestrator: env, queue loop, service wiring
  services/
    refinement_service.py            # per-item pipeline (refine())
    workspace_service.py             # clone + cache symlink + graphify extract
    context_service.py               # ADO comments + prompt template render
    publishing_service.py            # all ADO writes + attachment + tag transitions
  repository_knowledge.py            # facade + Graphify backend + Filesystem fallback
  repository_context.py              # curated prompt block builder (entity extract → context)
  ado_client.py                      # ADO REST (WIQL, JSON Patch, comments, attachments)
  pi_runner.py                       # Pi subprocess wrapper (retry on transient)
  git_ops.py                         # parallel shallow clone + per-clone PAT header
  validate.py                        # JSON-schema + sourceRef file existence check
  retry.py                           # 3 attempts, 1s/2s/4s, opt-in by exception class
  metrics.py                         # counters + timer CMs (Prom/OTel-ready)
  prompts/refine.prompt.tmpl.md      # the agent's prompt
  schema/findings.schema.json        # contract for Pi's output
  repos.jsonc                        # `repo:<name>` tag → git URL mapping
Dockerfile, run.ps1, check.ps1       # container build + Windows launcher + preflight
tests/                               # 13 files, ~2.8k lines, mocks at module boundaries
docs/MIGRATION.md, ONBOARDING.html   # 5-phase refactor narrative; embedded onboarding doc
```

## The 5 things worth understanding cold

1. **`src/refine.py:main()`** — thin. Loads `Config` from env, builds services once, runs `client.query_items(tag_trigger, [tag_done, tag_blocked])`, iterates items calling `RefinementService.refine(item)`. **Exit-code semantics are the whole game**: `pi_runner.InfraError` → exit 1 (infra failure); `validate.ValidationError` → add blocked tag, exit stays 0 (pipeline worked, content didn't); any other `Exception` is uncaught and exits 1 with a traceback.

2. **`src/services/refinement_service.py:RefinementService.refine()`** — the per-item pipeline in 7 steps: `resolve_repos` → `workspace.prepare` (with retry on git failures) → `context.build_inputs` (with curated `RepositoryContext` spliced in) → `pi_runner.run` → `validate.check` (NOT retried) → `publishing.publish` → `workspace.cleanup` (always, in `finally`). Metrics live here — counters for success/blocked/infra-failure, timers per phase. The `_record_duration` helper is the ugly part (see dragons).

3. **`src/services/publishing_service.py`** — owns every ADO write. `_publish_done` patches Description + AC (+ Title if `ALLOW_TITLE_EDITS=true` + `suggested_title` present), posts a summary comment, removes trigger tag, adds `tag_done`. `_publish_blocked` posts unknowns-list comment, adds `tag_blocked`. `_upload_attachment` runs first so a publish never half-completes with no attachment. Each write goes through `with_retry` against a narrow `_RETRYABLE_REST` tuple. `findings_to_ac_html` is currently a stub returning `<!-- auto-derived from refinement; review before sign-off -->` — the real AC shape isn't defined.

4. **`src/repository_knowledge.py`** — facade with `KnowledgeBackend` ABC, two implementations: `GraphifyBackend` (default, reads the parsed AST graph at `<repo>/graphify-out/graph.json` directly — no per-query CLI calls) and `FilesystemBackend` (grep-based, returns `degraded=True` markers on every curated op). `make_knowledge()` auto-selects. `_load_graph` supports two layouts: single-repo OR workspace-of-cloned-repos (merges per-repo graphs, namespace-prefixes node IDs to avoid collisions, re-roots `source_file` to absolute paths under the repo root). First structural call auto-extracts if the index is missing. Curated ops on the base class all return `degraded=True` defaults — `GraphifyBackend` is what makes them real.

5. **`src/repository_context.py`** — the curated kickstart. `_extract_entities` is biased hard toward code-shaped tokens (must have an internal capital letter, underscore, or digit; otherwise dropped). Stopwords cover EN + DE explicitly (the German article problem was the production example — `gesperrter`, `Ordering`, `Tag`, `ist`, `div` all get filtered). `build()` queries the backend defensively (`_safe_*` swallows exceptions); a broken graphify never breaks the prompt. The rendered section is deliberately small: architecture summary, a `/graphify query/path/explain/affected` cheatsheet, and the relevant files. The full dep graph is on disk; Pi is told to query it via the skill, not regurgitate it inline.

## Conventions / fingerprints

- **`ponytail:` comments** are the load-bearing convention. Every deliberate simplification or "this exists because X" is tagged. Read those before reading the function — they're faster and more honest than the function.
- **Module-attribute imports** at boundaries that tests monkeypatch: `import git_ops`, `import pi_runner`, `import validate` (not `from X import Y`). Breaking this convention breaks every test that spies through `monkeypatch.setattr(refine.git_ops, ...)`.
- **Self-checks at module bottom** (`if __name__ == "__main__"`): framework-free, fail-fast, one-shot. Trivially runnable.
- **`from __future__ import annotations`** + `dataclass(frozen=True)` everywhere. No mutable shared state in services.
- **Tests live in `tests/`**, `conftest.py` prepends `src/` to `sys.path`. No fixtures framework, no per-class setup; just `MagicMock()` + `monkeypatch.setattr` on module attributes.
- **PowerShell launchers** (`run.ps1`, `check.ps1`) wrap the Python; the Python never knows it's in a container. `check.ps1` is the preflight — catches missing `.env`, inline `#` comment pollution in PATs, missing image, no container engine, no Pi auth.json.
- **`/graphify` skill** is registered into Pi at image-build time (`graphify pi install`). Pi is told to use it for deeper exploration; the prompt explicitly forbids re-discovering with `grep`/`find`/`ls`.
- **Retry policy is opt-in by exception class**, not opt-out. Every site that retries passes its own tuple. `validate.check` failures are never in any retryable tuple.

## Dragons / risk map

- **`findings_to_ac_html` is a stub** in `publishing_service.py` (line ~158). AC writes go to ADO but the content is a literal placeholder string. Until the AC shape is decided, the AC field is effectively decorative.
- **`_record_duration` in `refinement_service.py`** defines `_on` then immediately abandons it and returns `_record`, which writes to the private `MetricsCollector._timings_ms` directly. Comment: "private but only this class writes it." Fragile — any second metrics-using class will silently break ordering or step on this. Cleanup candidate when touched.
- **Working tree is dirty**: `refinement_service.py`, `LICENSE`, `.gitignore` all modified. The `.gitignore` and `LICENSE` diffs are pure CRLF→LF line-ending normalization; `refinement_service.py` looks like the same. Likely an editor or `.gitattributes` change. Stash or commit before any further work — these changes will dirty every diff stat.
- **One uncaught-exception gap**: `RefinementService.refine()` only knows how to handle `pi_runner.InfraError` (→ `infra_failures_total`) and re-raises everything else (→ `blocked_refinements_total`). `main()` catches `InfraError` and `ValidationError` explicitly. A `RuntimeError` or `OSError` from anywhere else bubbles up uncaught from `main()` → exit 1 with traceback. Worth a narrower guard or explicit "expected" exception set.
- **`publishing_service._safe`** catches `Exception` broadly inside per-write retry. If `_comment` fails after retries, the function logs and continues — but `add_tag(blocked)` still fires afterwards. The work item ends up tagged blocked with no explanation comment. Subtle.
- **`repos.jsonc` only has one entry** (`Laekkerai.Ordering`). Every new repo requires an operator edit + container restart. The file is mounted `:ro` from the host.
- **CRLF line-ending drift**: only the three modified files are CRLF; everything else is LF. No `.gitattributes` enforcing a style — editor roulette.
- **Pre-existing test failure** (`tests/test_ado_client.py::test_query_items_uses_wiql_and_fetches_batch`) noted in MIGRATION as an API_VERSION URL prefix mismatch, left untouched. Anyone touching `ado_client.query_items` will inherit this.
- **`/tmp/ado-non-json-response.html`** — debug artifact dumped on ADO non-JSON response (proxy intercept, auth challenge). Useful, but it's an unwritten file in the container filesystem that won't get cleaned up.
- **`_extract_entities` is harsh to non-code text**. A ticket titled entirely in German prose produces an empty entity list → `relevant_files=None` in the prompt. Pi then has no curated files to anchor on. Documented behavior, but worth knowing.

## Trace: one item end-to-end (real flow, not the docs')

`./run.ps1` → container starts → `python -u src/refine.py` → `Config.from_env()` → `AdoClient` constructed → `repos_map = _load_jsonc(REPOS_CFG)` (regex strip of `//` line comments + trailing commas) → `client.query_items("needs-refinement", ["refinement-done", "refinement-blocked"])` posts WIQL, batches results in groups of 200 → for each item: `workspace_svc.prepare(item_id, repos, depth, pat)` clones repos in parallel (ThreadPoolExecutor, per-clone `GIT_CONFIG_*` auth header so PAT never lands in the remote URL or process list) into a shared cache root, then symlinks them into `/tmp/refine-<id>` → `WorkspaceService._sync_graphify_indexes` runs `graphify extract --code-only --no-cluster` per repo (no LLM call, AST only) → `RefinementService._build_repo_context` constructs `RepositoryContextBuilder(knowledge)`, extracts entities from item text, calls `architecture_summary` + `relevant_files` defensively, renders a small markdown block → `ContextService.build_inputs` loads top-20 ADO comments, then `str.replace` substitutes all placeholders into the prompt template (the live JSON schema is appended verbatim into `{schema}`) → `pi_runner.run` invokes `pi -p "<prompt>" --model <model>` as a subprocess with `with_retry` against transient errors; non-zero exit or non-JSON stdout raises `InfraError` → `validate.check` runs `Draft7Validator` against the schema and confirms every `sourceRef` resolves to a real file under the workspace → `PublishingService.publish` uploads the result markdown as an attachment (UTC-stamped, sanitized title), dispatches to `_publish_done` or `_publish_blocked` based on `unknowns`, finally `_safe(add_tag)` transitions the trigger tag to done/blocked → `workspace.cleanup` runs unconditionally in `finally` (rmtree `/tmp/refine-<id>`) → next item → finally `workspace_svc.shutdown` rmtree's the shared cache root.

## Open questions (would ask a teammate)

- The uncommitted CRLF diff — accidental editor swap, or are you about to commit a `.gitattributes` to pin LF?
- Is the `findings_to_ac_html` stub intentional (waiting on AC shape spec) or forgotten? It writes a placeholder on every successful publish today.
- Auto-retry on `refinement-blocked` is on the README's open-questions list. Any decision yet, or still manual?
- Why is `repos.jsonc` mounted `:ro` rather than baked into the image? Suggests ops expects frequent registry changes.
- The Bearer-token priority chain (`SYSTEM_ACCESSTOKEN` → `ADO_AUTH_TOKEN` → `ADO_MCP_AUTH_TOKEN` → `ADO_API_KEY`) is documented in `.env.example` but `AdoClient.__init__` only takes one PAT. Is there an alternate auth path coming, or dead documentation?
- `_record_duration` writes to private `_timings_ms` — okay as long as `RefinementService` is the only metrics user, but worth knowing it would break the moment anything else touches the collector.
- The `_publish_blocked` path's `add_tag(blocked)` runs after a best-effort `_comment` — if comments fail, the work item ends up tagged blocked with no human-readable explanation. Is that intentional?
- The pre-existing `test_ado_client.py` failure — is anyone planning to fix it, or is it frozen as "known broken, unrelated"?
- Single ADO project only — does the org use multiple ADO projects for the same repos, or is one project the universal scope?
