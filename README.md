# ticket-refinery

Containerized, deterministic pipeline that refines Azure DevOps work items
tagged `needs-refinement` using the Pi coding agent (read-only), then writes
findings back to the work item (description, AC, comment, tag transition).

## How it works

1. Query ADO for items tagged `needs-refinement`, excluding
   `refinement-done` / `refinement-blocked`.
2. Parse `repo:<name>` tags on the item, resolve against `repos.jsonc`.
3. Shallow-clone those repos into `/tmp/refine-<id>` (parallel within an item)
   and run `graphify install` against each so the AST index is fresh.
4. Gather curated repository intelligence — architecture summary, relevant
   files, dependency graph, execution paths, impact analysis — and embed it
   directly in the Pi prompt. Pi reasons over provided context instead of
   spending tool calls re-discovering the repository.
5. Run Pi (read-only permission profile) against the curated prompt.
6. Validate findings JSON (schema + every `sourceRef` resolves to a real file).
7. Patch description / AC inside a bounded HTML-comment block; comment once;
   upload an attachment with the markdown result; transition
   `needs-refinement` → `refinement-done` (or `refinement-blocked` on
   unknowns / validation failure).
8. Exit 0 when the queue is empty.

`process_item` is wrapped in a `try / finally` so the workspace is cleaned up
even on Pi / clone / ADO failures (which are retried with exponential backoff
before bubbling up as `InfraError`).

## `sourceRef` format

`repo/path/file.ext#Lline` — first segment is the repo name as registered in
`repos.jsonc`, rest is the path inside that repo. The `:` separator
(`repo:path#Lline`) is also accepted. Line range `#L5-L31` is optional.

## Prereqs

- ADO organization + project.
- Tags exist in ADO: `needs-refinement`, `refinement-done`, `refinement-blocked`.
  Override with `TAG_TRIGGER` / `TAG_DONE` / `TAG_BLOCKED` if you use different names.
- A PAT scoped to **Work Items Read & Write** and **Code Read**.
- Docker or Podman on PATH (or set `CONTAINER_ENGINE`).
- For graph-based repo exploration, the **Graphify** CLI on PATH (ships in
  the container image; the host doesn't need to install it).

The Dockerfile installs Graphify (`pip install graphifyy`) and runs
`graphify install` per-repo at item-prep time, so the running container is
self-contained. The host only needs Docker + `.env`.

## Quick start

```powershell
cp .env.example .env
# edit .env — set ADO_ORG, ADO_PROJECT, ADO_PAT
./run.ps1            # build local image, run
./run.ps1 -UseRemoteImage  # pull prebuilt, run
```

`run.ps1` fails fast if `.env` is missing. It does **not** fall back to
`.env.example` values (those are placeholders, not defaults meant for real use).

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  src/refine.py  (thin orchestrator)                                  │
│  - load Config from env                                              │
│  - construct services once                                           │
│  - query ADO queue, iterate items                                    │
└──────────────────────────┬───────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│  src/services/refinement_service.py  (per-item workflow)             │
│  ┌─────────────────┐  ┌──────────────┐  ┌────────────────────────┐   │
│  │ WorkspaceService │  │ ContextService │  │ PublishingService    │   │
│  │ - clone          │  │ - comments     │  │ - patches            │   │
│  │ - link cache     │  │ - render       │  │ - comments           │   │
│  │ - graphify sync  │  │ - target lang  │  │ - attachment upload  │   │
│  │ - cleanup        │  │               │  │ - tag transitions    │   │
│  └────────┬─────────┘  └──────┬───────┘  └───────────┬────────────┘   │
│           │                  │                       │                │
│           ▼                  ▼                       ▼                │
└──────────────────────────────────────────────────────────────────────┘
   │              │                          │
   ▼              ▼                          ▼
┌─────────┐   ┌──────────────┐   ┌─────────────────┐
│ git_ops │   │ validate.py  │   │  ado_client.py  │
│ + retry │   │ (schema +    │   │  + retry        │
│         │   │  sourceRefs) │   │                 │
└─────────┘   └──────────────┘   └─────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│  src/pi_runner.py  (subprocess, retried)     │
│  stdout → JSON                              │
└──────────────────────────────────────────────┘
```

**Cross-cutting layers** (independent of business logic):

```
┌─────────────┐   ┌────────────────┐   ┌──────────────────────────┐
│ src/retry.py│   │ src/metrics.py │   │ src/repository_knowledge  │
│ (transient  │   │ (counters +    │   │ (GraphifyBackend default, │
│  retry only)│   │  timers)       │   │  FilesystemBackend fbk)   │
└─────────────┘   └────────────────┘   └──────────────────────────┘
```

The pipeline is **one-way dependent**: `refine.py` → services → leaf
modules → retry/metrics/repo_knowledge. Services never import each other;
`RefinementService` is the only composer.

### Repository knowledge

`src/repository_knowledge.py` exposes `RepositoryKnowledge` (facade) with two
implementations behind the same `KnowledgeBackend` interface:

- **`GraphifyBackend`** (default): wraps the `graphify` CLI for structural
  queries (`symbol`, `callers`, `callees`, `references`, `implementations`,
  `impact`) and curated operations (`architecture`, `dependencies`,
  `execution-path`, `relevant`). Structural queries answer in O(1) from a
  parsed AST graph; curated operations compose them for direct LLM use.
- **`FilesystemBackend`** (fallback): grep-based, used only when Graphify
  is unavailable. Returns degraded markers on curated operations; cannot
  resolve call graphs.

The application talks to `RepositoryKnowledge` and never knows which backend
is active. Future backends (LSP, ctags, …) are injectable without touching
orchestration.

Repository intelligence is gathered **before** Pi runs. The orchestrator
builds a `RepositoryContext` (architecture summary, relevant files,
dependency graph, execution paths, impact) via `RepositoryContextBuilder`
and splices it into the Pi prompt. Pi reasons over the provided context
instead of re-discovering the repository.

### Retry policy (`src/retry.py`)

- 3 attempts max; delays 1s, 2s, 4s.
- Retried: git clone, ADO REST (WIQL/PATCH/POST), Pi subprocess, attachment
  upload.
- **Never retried**: schema validation, malformed JSON, unresolved
  `sourceRef`, business validation failures.
- Centralised — every call site goes through `retry.with_retry`.

### Metrics (`src/metrics.py`)

- Independent from logging. Snapshot-based, in-process.
- Captured: queue size, refinement / workspace prep / clone / prompt
  generation / Pi execution / validation / attachment upload / publishing
  durations. Counters: successful, blocked, infra failures, retries.
- Designed so a Prometheus exporter or OpenTelemetry meter can wrap it
  later without changing call sites.

## Layout

| Path                                          | Purpose                                                |
| --------------------------------------------- | ------------------------------------------------------ |
| `src/refine.py`                               | Thin orchestrator (config, DI, queue loop)             |
| `src/services/workspace_service.py`           | clone, cache, link, graphify sync, cleanup             |
| `src/services/context_service.py`             | existing comments + prompt rendering                   |
| `src/services/publishing_service.py`          | ADO writes (patches, comments, attachment, tags)       |
| `src/services/refinement_service.py`          | per-item workflow orchestration                       |
| `src/ado_client.py`                           | WIQL, JSON Patch, comments, marker-block edits         |
| `src/git_ops.py`                              | Concurrent shallow clone with per-clone credential hdr |
| `src/pi_runner.py`                            | Pi CLI subprocess wrapper (retried)                    |
| `src/validate.py`                             | JSON-schema + `sourceRef` existence check              |
| `src/retry.py`                                | Transient-retry helper                                 |
| `src/metrics.py`                              | Counters + timers (Prom/OTel-friendly)                 |
| `src/repository_knowledge.py`                 | Graphify-backed repo knowledge facade                  |
| `src/repository_context.py`                   | RepositoryContext DTO + builder                        |
| `src/schema/findings.schema.json`             | Findings contract                                      |
| `src/prompts/refine.prompt.tmpl.md`           | Pi prompt (curated RepositoryContext section)          |
| `src/repos.jsonc`                             | `repo:<tag>` → git URL mapping (structural, not env)   |
| `.env.example`                                | Every configurable env var, documented                 |
| `.env`                                        | Real values, git-ignored                               |
| `Dockerfile`                                  | Container image (Graphify + Pi preinstalled)           |
| `run.ps1`                                     | Thin PowerShell launcher                               |
| `check.ps1`                                   | Preflight diagnostic                                   |

## Exit codes

| Code | Meaning                                                          |
| ---- | ---------------------------------------------------------------- |
| `0`  | Success: queue empty or all items refined / marked blocked       |
| `1`  | Infra failure: auth, clone, Pi invocation, unhandled exception   |
| `2`  | Launcher error: missing `.env`, no container engine              |

Reaching `refinement-blocked` (unknowns, validation failure) is a **successful**
pipeline run. Only auth / clone / Pi failures should fail the container.

## Self-checks

Each module ships with a tiny `__main__` self-check (run `python src/validate.py` etc.). They are framework-free, fail-fast, and exist so the lazy code isn't blind.

## Open questions (flagged from the brief, deferred from v1)

- Auto-retry after human edits a `refinement-blocked` item? **v1**: manual
  removal of the tag is required to re-trigger. Auto-retry risks loops if the
  human's answer still doesn't resolve the unknown.
- DTO/API specs as separate ADO relations/attachments vs comment + description?
  **v1**: comment + description + attachment. Defer relations until product asks.
- Sparse clone via `sparsePaths` in `repos.jsonc`? **v1**: full shallow clone.
  Add `sparsePaths` field when a repo proves too large.

## Security notes

- PAT lives only in `.env` (git-ignored) and the container's env block.
- Never baked into the image, never passed as a CLI flag, never embedded in
  a clone URL — `git_ops.py` uses a short-lived `http.extraHeader` per clone.
- Pi runs under a read-only permission profile scoped to the per-item workspace.