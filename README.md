# ticket-refinery

Containerized, deterministic pipeline that refines Azure DevOps work items
tagged `needs-refinement` using the Pi coding agent (read-only), then writes
findings back to the work item (description, AC, comment, tag transition).

## How it works

1. Query ADO for items tagged `needs-refinement`, excluding
   `refinement-done` / `refinement-blocked`.
2. Parse `repo:<name>` tags on the item, resolve against `repos.jsonc`.
3. Shallow-clone those repos into `/tmp/refine-<id>` (parallel within an item).
5. Run Pi (read-only permission profile) against workspace + work item text.
6. Validate findings JSON (schema + every `sourceRef` resolves to a real file).
7. Patch description / AC inside a bounded HTML-comment block; comment once;
   transition `needs-refinement` → `refinement-done` (or `refinement-blocked`
   on unknowns / validation failure).
8. Exit 0 when the queue is empty.

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

## Quick start

```powershell
cp .env.example .env
# edit .env — set ADO_ORG, ADO_PROJECT, ADO_PAT
./run.ps1            # build local image, run
./run.ps1 -UseRemoteImage  # pull prebuilt, run
```

`run.ps1` fails fast if `.env` is missing. It does **not** fall back to
`.env.example` values (those are placeholders, not defaults meant for real use).

## Layout

| Path                                    | Purpose                                                  |
| --------------------------------------- | -------------------------------------------------------- |
| `src/refine.py`                         | Orchestrator: config, queue loop, exit codes             |
| `src/ado_client.py`                     | WIQL, JSON Patch, comments, marker-block edits           |
| `src/git_ops.py`                        | Concurrent shallow clone with per-clone credential header |
| `src/pi_runner.py`                      | Pi CLI subprocess wrapper                                |
| `src/validate.py`                       | JSON-schema + `sourceRef` existence check                |
| `src/schema/findings.schema.json`       | Findings contract                                        |
| `src/prompts/refine.prompt.tmpl.md`     | Pi prompt template                                       |
| `src/repos.jsonc`                       | `repo:<tag>` → git URL mapping (structural, not env)      |
| `.env.example`                          | Every configurable env var, documented                   |
| `.env`                                  | Real values, git-ignored                                 |
| `Dockerfile`                            | Container image                                          |
| `run.ps1`                               | Thin PowerShell launcher                                 |

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
  **v1**: comment + description. Defer relations until product asks.
- Sparse clone via `sparsePaths` in `repos.jsonc`? **v1**: full shallow clone.
  Add `sparsePaths` field when a repo proves too large.

## Security notes

- PAT lives only in `.env` (git-ignored) and the container's env block.
- Never baked into the image, never passed as a CLI flag, never embedded in
  a clone URL — `git_ops.py` uses a short-lived `http.extraHeader` per clone.
- Pi runs under a read-only permission profile scoped to the per-item workspace.