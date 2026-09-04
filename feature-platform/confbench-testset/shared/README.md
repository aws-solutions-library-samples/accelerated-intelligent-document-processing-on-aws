# Shared module — `variants.py`

`variants.py` is the ConfBench noise-variant catalog: exact per-variant file
counts and byte sizes, the named size tiers, and the selection→test-set-id
mapping. Three consumers need the identical numbers:

| Consumer | Uses it for |
|---|---|
| `feature-api/` | `GET /variants` (what the picker renders), request validation |
| `ingest/` (planner) | filtering the parquet to the selected variants, expected totals |
| `feature-ui/` | rendering sizes — fetched from the API at runtime, never hardcoded |

It ships as a **Lambda layer** (`SharedLayer` in `template.yaml`) rather than
being copied into each function's `CodeUri`. A duplicated copy is the failure
mode worth designing against here: if the API's table and the planner's table
drift, the cost estimate an admin approves stops matching the bytes that
actually land — the exact problem this extension exists to solve.

The module lives at `shared/python/variants.py`. Lambda adds a layer's `python/`
directory to `sys.path`, so both functions `import variants` directly.

The layer resource deliberately has **no `BuildMethod: python3.12` metadata**:
that build method runs pip against a `requirements.txt` and packages only
installed dependencies, which would silently drop this hand-authored module.
Without it SAM copies `shared/` verbatim, which is what we want.

## Layer size is gated on the *update path*, not the steady state

The layer also carries `pyarrow` (see `Makefile`), which makes its size a
deployment concern rather than a footnote. `prune_layer.py` runs after the pip
install, strips the ~35 MB of pyarrow that `pyarrow.parquet` never touches
(`include/`, `src/`, `tests/`, Cython sources, the Arrow Flight RPC stack), and
then **fails the build** if the result is still too big — 136 MB → 100 MB, against
a 117.6 MB ceiling.

That ceiling is not the obvious one. Steady state is only ~109 MB (layer + the
8.5 MB `ingest/` code), nowhere near Lambda's 250 MiB cap. The binding constraint
is the *transition*: CloudFormation attaches the new layer via
`UpdateFunctionConfiguration` **before** `UpdateFunctionCode`, so mid-update the
new layer is combined with the OLD code — and feature versions before 2026-08-13
bundled pyarrow into `ingest/` itself (144,555,306 bytes). New layer + old code
= 280,347,130 bytes, which is exactly how the update of a stack installed at
0.6.3.dev6 failed. Hence `LAYER_CEILING = 262,144,000 - 144,555,306`.

Pruning by filename is fragile across pyarrow releases, so the script refuses to
run blind. Two guards, both covered by `tests/test_prune_layer.py`:

| Guard | Catches |
|---|---|
| every prune rule must match ≥1 file | a pyarrow layout change silently pruning nothing, so the layer quietly regrows |
| no surviving `.so` may have a `DT_NEEDED` on a pruned library | a future pyarrow linking Flight from the parquet path — fails the build instead of the Lambda cold start |

Do not widen the prune to `libarrow_substrait`, `libarrow_dataset`,
`libarrow_acero`, or `libarrow_compute`: `lib.…so` and `_parquet.…so` link all
four directly, and removing any of them breaks `import pyarrow` outright.

## Keeping it honest

`verify_against_hub()` re-derives the whole table from the live HuggingFace tree
API and diffs it against the committed constants. `tests/test_variants.py` calls
it when `CONFBENCH_NETWORK_TESTS=1`, so an upstream re-publish surfaces as a
test failure instead of a wrong number in the UI. The figures were measured
2026-08-05 against `amazon/ConfBench` @ `main`.
