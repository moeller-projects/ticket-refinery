# Refactor Migration Summary

Five independent phases, each shippable on its own. All delivered in one cycle here.

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

## Phase 2 — Retry & Resilience

**Added**: `src/retry.py` — `with_retry(fn, *, retryable, delays, on_retry)`.

**Policy**: 3 attempts, exponential backoff (1s, 2s, 4s). Retries are
**explicitly opt-in by exception class** — every call site passes
`(ConnectionError, TimeoutError, OSError, …)`. Auth errors and validation
errors are NOT in any retryable tuple.

## Phase 3 — Metrics

**Added**: `src/metrics.py` — `MetricsCollector` with `increment(name,
value)`, `timer(name)` context manager, `snapshot()` returning an
immutable `MetricsSnapshot`. Wired into `RefinementService` only.

## Phase 4 — CodeGraph-powered Repository Exploration

**Added**: `src/repository_index.py`.

```
RepositoryExplorer                ← facade
   ↓
ExplorerBackend (ABC)
  ├── CodeGraphBackend             ← default
  └── FilesystemBackend           ← fallback (used only when codegraph is unavailable)
```

## Phase 5 — Migrate to Graphify + curate context in the orchestrator

**Replaces Phase 4** wholesale. The repository intelligence layer was
refactored from CodeGraph to Graphify, the abstraction renamed to
`RepositoryKnowledge`, and repository intelligence moved out of the Pi
prompt into the orchestration layer.

### Renames

| Phase 4 name            | Phase 5 name           |
| ----------------------- | ---------------------- |
| `repository_index.py`   | `repository_knowledge.py` |
| `RepositoryExplorer`    | `RepositoryKnowledge`  |
| `ExplorerBackend`       | `KnowledgeBackend`     |
| `CodeGraphBackend`      | `GraphifyBackend`      |
| `make_explorer`         | `make_knowledge`       |

Legacy aliases (`RepositoryExplorer = RepositoryKnowledge`,
`ExplorerBackend = KnowledgeBackend`, `CodeGraphBackend = GraphifyBackend`)
remain in `repository_knowledge.py` so any partial migration doesn't break
imports.

### New abstraction surface

`RepositoryKnowledge` (the facade) exposes curated operations on top of the
low-level structural queries:

| Operation                | Purpose                                            |
| ------------------------ | -------------------------------------------------- |
| `search`                 | literal text                                       |
| `find_symbol`            | AST symbol lookup                                  |
| `find_callers` / `find_callees` | inverse / forward call graph                 |
| `find_references`        | identifier references                              |
| `find_implementations`   | class hierarchy                                    |
| `impact_analysis`        | change blast radius                                |
| `architecture_summary`   | human-readable summary + module list (curated)     |
| `dependency_graph`       | structured module + edge graph (curated)           |
| `execution_path`         | call chain for one symbol (curated)                |
| `relevant_files`         | files most likely to answer `query` (curated)      |

The curated ops are concrete defaults on `KnowledgeBackend` (returning
`degraded=True` markers); `GraphifyBackend` overrides them with real
`graphify architecture / dependencies / execution-path / relevant`
subcommands. `FilesystemBackend` keeps the defaults plus a degraded
`relevant_files` that falls back to literal grep.

### Repository intelligence moved into the orchestrator

```
ADO work item
   ↓
RepositoryKnowledge  (per-item, points at workspace path)
   ↓
RepositoryContextBuilder.build(item)
   ↓
RepositoryContext  (architecture + dependencies + execution paths
                    + relevant files + impact, plus `degraded` flag)
   ↓
ContextService.build_inputs(..., repo_context_section=...)
   ↓
Pi  ← reasons over provided context; no exploration tools
```

The prompt template (`src/prompts/refine.prompt.tmpl.md`) replaces the
previous "Repository exploration — MANDATORY ORDER" section with a
"Repository context (curated)" section. Pi no longer needs to call any
exploration tools itself; it consumes curated content and only uses `read`
on listed files for specific line verification.

### `RepositoryContextBuilder`

`src/repository_context.py` — small, stateless, pure function. Extracts
candidate entities (camel/snake-split, stopword-filtered) from
`item["fields"]["System.Title" | "System.Description" |
"Microsoft.VSTS.Common.AcceptanceCriteria" | "Microsoft.VSTS.TCM.ReproSteps"
| "Microsoft.VSTS.TCM.SystemInfo"]` and queries the backend for curated
operations. All backend calls are wrapped in defensive try/except so a
misbehaving Graphify install never breaks the prompt — degraded markers
flow through to the rendered context section.

### Filesystem fallback preserved

When `graphify` is missing on PATH:
- `make_knowledge()` auto-selects `FilesystemBackend`.
- Low-level ops answer via grep (existing behaviour).
- `relevant_files` answers via grep but flags `degraded=True` so the
  orchestrator can warn Pi that the result is approximate.
- Curated ops (`architecture_summary`, `dependency_graph`,
  `execution_path`) return `degraded=True` markers with empty payloads;
  Pi's prompt template handles this by telling Pi to verify with `read`
  when the context is degraded.

The application never fails solely because Graphify is unavailable.

### Tests

| Test file                                  | Coverage                                  |
| ------------------------------------------ | ----------------------------------------- |
| `tests/test_repository_knowledge.py`       | facade + GraphifyBackend + FilesystemBackend + curated ops + DTOs + legacy aliases |
| `tests/test_repository_context.py`         | entity extraction, build(), prompt rendering, degraded paths, error swallowing |
| `tests/test_prompt.py`                     | placeholder presence, absence of codegraph_* tools, repo_context substitution |
| `tests/test_context_service.py`            | `repo_context_section` parameter (splice + empty) |
| `tests/test_refinement_service.py`         | knowledge injection → curated context → ContextService |

### Why no service locator / no global state

- Services are constructed explicitly in `main()` and `process_item()`.
  No module-level globals.
- `MetricsCollector` is **optional** (constructor kwarg, default `None`).
- `RepositoryKnowledge` is built per item via `make_knowledge(project_path=…)`.
  No global registry. One facade per work-item workspace.

## Backwards compatibility

- Public behaviour, CLI interface, environment variables, Docker, run.ps1,
  check.ps1, ADO schema, output markdown, work-item updates — all
  unchanged.
- `refine.process_item(item, cfg, client, repos_map, log, *,
  repo_cache_root=None, knowledge=None)` signature preserved for any
  external callers. `knowledge` is a new optional kwarg.
- `Config` gains a `target_language: str = "English"` default.
- `make_explorer`, `RepositoryExplorer`, `ExplorerBackend`,
  `CodeGraphBackend` aliases remain in `repository_knowledge.py` so
  importers do not break during the rename.

## What was deliberately NOT done (ponytail: YAGNI)

- No `interface` for `AdoClient` — there is exactly one implementation.
- No `factory class` for backends — `make_knowledge()` is a function.
- No `Container` DI framework — explicit keyword args in `RefinementService.__init__`.
- No premortem abstraction for "Prometheus exporter" — that slots in when it's needed.
- No LSP / ctags backend — only Graphify and Filesystem, the two
  endpoints the brief names.
- No structured-logging rework — logs were untouched per the brief.

## Verified

```
$ python -m pytest -q
181 passed in 21.28s
```

(One pre-existing failure in `tests/test_ado_client.py::
test_query_items_uses_wiql_and_fetches_batch` exists before this
refactor — it's an API_VERSION URL prefix mismatch unrelated to the
migration. Left untouched per the brief's "do not break existing
behaviour" rule.)