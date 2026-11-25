---
name: create-hf-dataset-pr
description: "Use when contributing a data or label correction to an external HuggingFace dataset through a community PR: parquet key order, verification and review artefacts."
---

# Skill: Create a HuggingFace dataset PR (corrections / data changes)

Use when contributing a **data or label correction** to an external HuggingFace
dataset repo via a community Pull Request. Written from the RealKIE-FCC-Verified
GT-audit PR (see memory `project-realkie-gt-audit-pr`).

## Prerequisites

- `pip install huggingface_hub pandas pyarrow` (usually already present).
- A **write-scoped** HF token. Do NOT hardcode it. Prefer:
  - `huggingface-cli login` (interactive; token stays out of the transcript), or
  - `export HF_TOKEN=hf_...` for the command only.
- **Remind the user to rotate the token afterward** if it ever appeared in chat.
- Verify identity first: `python3 -c "from huggingface_hub import whoami; print(whoami()['name'])"`.

## Guardrails (this is an outward-facing, hard-to-reverse action)

- Get **explicit user approval** before pushing anything to an external public repo.
- Check the dataset **LICENSE** permits redistributing modified labels (e.g. CC-BY-NC
  allows derivatives w/ attribution + non-commercial).
- You almost never have write access to `main` of someone else's repo — you can only
  open a PR (`create_pr=True` / uploading to `revision="refs/pr/N"`). You cannot
  self-merge; it lands only when a maintainer merges.

## The critical gotcha: Parquet key/struct-order churn

Dataset GT usually lives inside a **binary Parquet** column (e.g. `json_response`),
NOT loose JSON. The naive `pd.read_parquet → edit dicts → df.to_parquet` round-trip
**alphabetically reorders JSON object keys and Arrow struct fields** on EVERY row of
EVERY struct column (e.g. an untouched `json_schema` column). Semantically harmless
(key order isn't significant) but it makes the diff huge and contradicts a
"only N cells changed" claim.

**Fix — edit at the pyarrow level and preserve the original struct type:**

```python
import pyarrow as pa, pyarrow.parquet as pq, json
t = pq.read_table("orig.parquet")
resp = t.column("json_response").to_pylist()      # list of dicts
resp_type = t.schema.field("json_response").type  # <-- reuse to preserve field order
# ... mutate resp in place, asserting each old value matches before writing new ...
new = pa.array(resp, type=resp_type)
t2 = t.set_column(t.column_names.index("json_response"), "json_response", new)
pq.write_table(t2, "corrected.parquet")
# leaves id/text/json_schema/image_files as the ORIGINAL untouched arrays
```

## Verify the diff is EXACTLY what you claim (before AND after upload)

Download both revisions and diff cell-by-cell — don't trust `list_repo_files`
(it lists the whole tree, not the diff):

```python
a = pd.read_parquet(f"hf://datasets/{repo}@main/data/....parquet")
b = pd.read_parquet(f"hf://datasets/{repo}@refs/pr/{N}/data/....parquet")
# assert only the intended (id,row,field) cells differ; all other columns byte-identical
# (normalize numpy types + sort_keys when comparing struct/schema columns)
```

## Submit

```python
from huggingface_hub import HfApi
api = HfApi()
commit = api.upload_file(
    path_or_fileobj="corrected.parquet",
    path_in_repo="data/test-00000-of-00001.parquet",
    repo_id=repo, repo_type="dataset",
    commit_message=TITLE, commit_description=BODY,  # BODY is the PR writeup
    create_pr=True,                                  # opens PR, returns .pr_url
)
```

- Add follow-up files to the SAME PR branch with `revision="refs/pr/N"`.
- Comment: `api.comment_discussion(repo_id, repo_type="dataset", discussion_num=N, comment=...)`
- Edit description/comment: `api.edit_discussion_comment(..., comment_id=..., new_content=...)`
- Status check: `api.get_discussion_details(...)` → `.status` is `draft|open|closed|merged`.
  **`open` = fully submitted** (not a draft); `draft` is a separate WIP state.

## Make a binary PR reviewable

A binary Parquet renders as "Binary file not shown" — reviewers can't see the change.
Add an in-tree `corrections/` folder on the PR branch:
- `CORRECTIONS.md` — human-readable before→after table with per-edit evidence
- `MANIFEST.json` — machine-readable `{doc,row,field,old,new,evidence}`
- a unified diff of pretty-printed records
- optional review images (bbox-annotated page renders). For **scanned PDFs (no text
  layer)** you can't use PyMuPDF `search_for`; OCR each page with Textract (boto3, raw
  bytes — the AWS CLI double-encodes and fails) and box the value via word geometry.
  Caveat: when a value repeats in a table the box marks *an* instance, not the exact row.

## Common failures

- `whoami` throws / "Not authenticated" → no token set.
- AWS CLI `textract detect-document-text --document '{"Bytes":"..."}'` → double-encodes;
  use boto3 with raw file bytes instead.
- Stray output files from a crashed run polluting a glob (e.g. an extra `*.pdf.json`) →
  re-verify counts after cleanup.
