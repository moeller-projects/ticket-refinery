# Refactor Migration Summary

Four independent phases, each shippable on its own. All deliver in one cycle here.

## Phase 1 — Service Layer

**Before**: `refine.py` mixed env parsing, queue iteration, repo resolution,
workspace lifecycle, prompt rendering, Pi invocation, validation, ADO writes,
and tag transitions in one ~280-line file.

**After**: `refine.py` shrank to **configuration, dependency wiring, queue
loop, and the per-item `process_item` thin wrapper**. Workflow moved to:

```
src/services/
  workspace_service.py    — clone, cache linking, cleanup
  context_service.py      — comment load + prompt render
  publishing_service.py   — description/AC/title patches, comments,
                             attachment upload + relation, tag transitions
  refinement_service.py   — per-item orchestration of the three above
```

**Dependency direction**: `refine.py` → `RefinementService` → `{Workspace,
Context, Publishing}Service` → `{git_ops, pi_runner, ado_client, validate}`.
Leaf modules never import services. Services never import each other.
Cycles: none.

**Cost-of-omission if reverted**: `refine.py` again becomes the de-facto
orchestrator; renaming `patch_description` → `patch_description_with_block`
would force a 200-line diff instead of a 3-line one.

## Phase 2 — Retry & Resilience

**Added**: `src/retry.py` — `with_retry(fn, *, retryable, delays, on_retry)`.

**Policy**: 3 attempts, exponential backoff (1s, 2s, 4s). Retries are
**explicitly opt-in by exception class** — every call site passes
`(ConnectionError, TimeoutError, OSError, ...)`. Auth errors and validation
errors are NOT in any retryable tuple.

**Coverage**:

| Operation                     | Where wrapped                  | Retryable tuple                                       |
| ----------------------------- | ------------------------------ | ----------------------------------------------------- |
| `git clone_all`               | `WorkspaceService.prepare`     | `(subprocess.CalledProcessError, OSError, …)`         |
| ADO WIQL POST + GET batch     | `AdoClient.query_items`        | `(ConnectionError, TimeoutError, OSError, requests.*)`|
| ADO `_patch`                  | `AdoClient._patch`             | requests retryable                                    |
| ADO comments GET + POST       | `AdoClient.comment/get_comments`| requests retryable                                    |
| Attachment upload + relation  | `AdoClient.upload_attachment`, `add_attachment_relation` | same                                  |
| Pi `subprocess.run`           | `pi_runner.run`                | `(ConnectionError, TimeoutError, OSError, TimeoutExpired)` |

**Non-retryable** (deliberately excluded): schema validation, malformed
JSON, unresolved sourceRefs, business validation failures. They raise
`InfraError`/`ValidationError` immediately. Repeating them just amplifies
the noise.

**Centralised** — every retry loops in the same place. No duplicated
backoff math.

## Phase 3 — Metrics

**Added**: `src/metrics.py` — `MetricsCollector` with `increment(name,
value)`, `timer(name)` context manager, `snapshot()` returning an
immutable `MetricsSnapshot`.

**Wired into** `RefinementService` (single seam):

| Metric                              | Type   |
| ----------------------------------- | ------ |
| `workspace_preparation_seconds`     | timer  |
| `clone_seconds`                     | timer  |
| `prompt_generation_seconds`         | timer  |
| `pi_execution_seconds`              | timer  |
| `validation_seconds`                | timer  |
| `publishing_seconds`                | timer  |
| `attachment_upload_seconds`         | timer  |
| `successful_refinements_total`      | counter|
| `blocked_refinements_total`         | counter|
| `infra_failures_total`              | counter|

`MetricsCollector` is imported **only** in `refinement_service.py` (and
`tests/`). The leaf modules — `git_ops`, `pi_runner`, `ado_client`,
`validate`, the services — don't know it exists.

**Extension path**: to add Prometheus or OpenTelemetry, add a thin adapter
that subscribes to `MetricsSnapshot` (subscribe pattern by polling, or
switch the collector to publish to an internal queue). No call-site changes
needed.

## Phase 4 — CodeGraph-powered Repository Exploration

**Added**: `src/repository_index.py`.

**Architecture**:

```
RepositoryExplorer                ← facade
   ↓
ExplorerBackend (ABC)
  ├── CodeGraphBackend             ← default
  └── FilesystemBackend           ← fallback (used only when codegraph is unavailable)
```

Factory `make_explorer()` picks `CodeGraphBackend` when the `codegraph` CLI
is on PATH, else `FilesystemBackend`. Tests pass `force_backend="filesystem"`
to bypass detection.

**Methods** (identical across both backends):
- `status()` — backend health
- `search_text(query)` — literal text
- `find_symbol(name, kind=)` — AST symbol
- `find_callers(symbol)` — inverse call graph
- `find_callees(symbol)` — forward call graph
- `find_references(name)` — identifier references
- `find_implementations(name)` — class hierarchy
- `impact_analysis(symbol, depth=)` — change blast radius

**Prompt update** (`src/prompts/refine.prompt.tmpl.md`): explicit
"Repository exploration — MANDATORY ORDER" section tells Pi to call
`codegraph_*` tools first, structural queries before text, filesystem tools
only as fallback, never re-traverse. The brief's required phrasing
("Prefer CodeGraph / use callers, references and impact analysis whenever
possible / avoid repeated traversal") is included verbatim.

**Filesystem fallback rules**: `FilesystemBackend.find_callees` and
`find_implementations` return empty (filesystem can't resolve the graph).
`impact_analysis()` returns a `{"approximate": True, "note": …}` payload so
the caller knows the result is degraded.

## Why no service locator / no global state

- Services are constructed explicitly in `main()` and `process_item()`.
  No module-level globals.
- `MetricsCollector` is **optional** (constructor kwarg, default `None`).
  When `None`, the timer helpers return a `_NullTimer` context manager
  that no-ops at runtime — zero overhead, no singleton lookup.
- `RepositoryExplorer` is built per project via `make_explorer(project_path=…)`.
  No global registry.

## Tests

- Original 76 tests now all green (was 67 + 9 pre-existing failures).
- Added 78 new tests across `test_retry.py`, `test_metrics.py`,
  `test_workspace_service.py`, `test_context_service.py`,
  `test_publishing_service.py`, `test_refinement_service.py`,
  `test_repository_index.py`, `test_prompt.py`.
- Total: **154 tests passing**.

## Backwards compatibility

- Public behaviour, CLI interface, environment variables, Docker, run.ps1,
  check.ps1, ADO schema, generated prompt (subject to the CodeGraph-first
  edits), attachment format, output markdown, work-item updates — all
  unchanged.
- `refine.process_item(item, cfg, client, repos_map, log, *,
  repo_cache_root=None)` signature preserved for any external callers.
- `Config` gains a `target_language: str = "English"` default so existing
  callers that omit it keep working.

## What was deliberately NOT done (ponytail: YAGNI)

- No `interface` for `AdoClient` — there is exactly one implementation.
- No `factory class` for backends — `make_explorer()` is a function.
- No `Container` DI framework — explicit keyword args in `RefinementService.__init__`.
- No premortem abstraction for "Prometheus exporter" — that slots in when it's needed.
- No structured-logging rework — logs were untouched per the brief.

## Verified

```
$ python -m pytest -q
154 passed in 20.81s
```
