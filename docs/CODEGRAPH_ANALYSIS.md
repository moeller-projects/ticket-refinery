# CodeGraph deep analysis — `ado-refinement-engine`

Snapshot from a full codegraph traversal: 7 prod modules, 7 test files,
~965 LoC of tests, 82 test cases. Tight surface, clear layering.

## Architecture

```
refine.py            orchestrator: env → queue loop → process_item → write back
├── ado_client.py    ADO REST: WIQL, JSON Patch, comments, attachments
├── git_ops.py       parallel shallow clone with per-clone PAT header
├── pi_runner.py     subprocess wrapper for `pi -p`
├── validate.py      jsonschema + sourceRef existence check
├── marker.py        sha1(title + description + sorted HEAD SHAs)
└── (schema/, prompts/, repos.jsonc, .env)
```

**Hot paths** (from `codegraph_callers`/`codegraph_impact`):

- `process_item` — single big function (~75 LoC). 6 dedicated tests + 1 symlink test.
- `AdoClient` — 13 call sites in `refine.py`. Only `_replace_block` and the
  comment URL have test drift.
- `pi_runner.run` — 1 caller (`process_item`).
- `marker.compute` — **0 callers in production code**. Orphan module. Tests
  cover it but prod never imports it.

## Critical findings

### 1. Marker idempotency is not wired — 6 tests fail because of this one root cause

`refine.py` never imports `marker`. README step 4 ("Compute idempotency
marker; skip if unchanged") and step 8 ("Store the marker in `MARKER_FIELD`")
are **not implemented**.

Evidence in tests, **absent in production**:

- `tests/test_refine.py:259` `monkeypatch.setattr(refine.marker, "compute", ...)`
  → `AttributeError: module 'refine' has no attribute 'marker'`.
- `tests/test_refine.py:189` expects `MARKER_FIELD` env var to be parsed into
  `cfg.marker_field` — but `Config` has no `marker_field` field
  (`refine.py:39–58`) and `Config.from_env` doesn't read `MARKER_FIELD`.
- `tests/test_refine.py:309` expects `client.set_field(42, cfg.marker_field, "NEW")`
  to be called on success.
- `tests/test_refine.py:335` expects `client.set_field.assert_not_called()` when
  marker unchanged → skip path.

`AdoClient.set_field` exists (`ado_client.py:206`), `add_tag`/`remove_tag`
exist. The pieces are all present, just never assembled.

Cascade: 6 of 10 test failures trace back to this one missing import + one
missing field + one missing `MARKER_FIELD` env read.

### 2. ADO URL drift — test vs prod disagree

| Test expects                                            | Production emits                                              |
| ------------------------------------------------------- | ------------------------------------------------------------- |
| `…/wit/workitems/11/comments`                           | `…/wit/workItems/11/comments`                                 |
| `_apis/wit/wiql?api-version=7.1-preview.4`              | `_apis/wit/wiql?api-version=7.1`                              |

`AdoClient.comment` hardcodes `workItems` (line 119); `query_items`
hardcodes `api-version=7.1` (line 73) instead of using
`API_VERSION = "7.1-preview.4"`. WIQL and PATCH endpoints both use `7.1`
directly — inconsistent with the rest of the file. Fix: route WIQL and PATCH
URLs through `API_VERSION` and fix `workItems` casing.

### 3. `pi_runner.run` test-pokes a real `node` binary

`pi_runner.py:30` runs `subprocess.check_output(["node", "-v"], ...)`
**before** the mocked `subprocess.run`. The test patches `subprocess.run` but
not `check_output`, so the `FileNotFoundError` from the test's fake leaks out
as a real `FileNotFoundError`. Either move the probe inside the `try` or
behind a guarded helper.

### 4. `pi_runner.run` CLI shape drifted from the test expectation

Test (`tests/test_pi_runner.py:35`) expects
`["pi", "-p", "PROMPT", "--model", "model"]`.
Production (`pi_runner.py:21`) emits
`["pi", "-p", "--tools", "read,bash,grep,find,ls", "--model", "model", prompt]`.

The `--tools` lock-down was added after the test was written. Either rewrite
the test to assert the new shape, or drop `--tools` — it's already enforced by
`pi-permissions.refinement.jsonc` per the README.

## Smaller issues

- `findings_to_ac_html` (`refine.py:168`) returns a stub string. Tests assert
  it. Honest about the deferral via an in-source `ponytail:` comment.
- `_link_repo_cache` is wired. `git_ops.cleanup` runs per item on `workspace`
  (symlinks only); `main()`'s `finally` cleans `repo_cache_root` once at end.
  Correct, just non-obvious — read the `try`/`finally` carefully before
  editing either side.
- `_clean` strips `# comment` only when preceded by whitespace
  (`re.sub(r"\s+#.*$", "", v)`). Won't strip a `#` glued to a value — correct,
  since PATs and URLs can legitimately contain `#`. Good defense, well-tested.
- `validate._ref_resolves` has a fallback that searches under all
  `known_repos` when a bare path is given (line 73). Useful, but the fallback
  uses `path_part` as-is, not the `repo` slice — could match unintended files.
  Worth a test (none exists).
- `comment`/`tag` writes are best-effort
  (`except Exception as comment_err: log.warning(...)`) — comment failures
  won't block tag transition. Good.

## Test coverage map

| Module        | Tests | Status                                           |
| ------------- | ----- | ------------------------------------------------ |
| `ado_client`  | 15    | 13 pass, 2 fail (URL drift)                      |
| `git_ops`     | 7     | all pass                                         |
| `marker`      | 5     | all pass — **but module unused in prod**         |
| `pi_runner`   | 5     | 3 pass, 2 fail (CLI drift + node probe)          |
| `refine`      | 24    | 19 pass, 5 fail (marker wiring)                  |
| `validate`    | 8     | all pass                                         |
| **Total**     | **82**| **72 pass, 10 fail**                             |

## Ponytail cuts (low-risk wins, none required)

- `_make_auth` (`ado_client.py:14`) is only called from `__init__` — inline.
- `print(... file=sys.stderr)` debug blocks in `query_items`
  (`ado_client.py:64–85`) duplicate what `logging` would do — swap for
  `log.info`.
- `versioned_attachment_name` reads `datetime.now()` per item — fine, but
  could be parameterised for testability (no test currently).
- `repos.jsonc` has a trailing comma and an inline `//` comment —
  `_load_jsonc` handles both correctly; no change needed.

## Recommended fix order

1. **Wire `marker` into `process_item`**: `import marker`, add
   `marker_field: str` to `Config`, read `MARKER_FIELD` in `from_env`,
   compute marker after comments, skip if equal to
   `item['fields'][cfg.marker_field]`, otherwise `client.set_field` after the
   write-back. Fixes 6 tests, completes README steps 4+8.
2. **Normalize ADO URLs** to `API_VERSION` + `workitems` casing in
   `ado_client.py`. Fixes 2 tests.
3. **Guard or move the `node -v` probe** in `pi_runner.py`. Fixes 1 test.
4. **Decide on `--tools` flag** in `pi_runner.run` and align the test.
   Fixes 1 test.

After those 4 edits: 82/82 green, prod matches README, marker idempotency
actually works.

Skipped: factoring `process_item` into smaller units (75 LoC, one screen,
reads top-to-bottom — current shape is fine), converting `print(...,
file=sys.stderr)` to logging (cosmetic), adding per-section URL builders (one
constant change is enough).
