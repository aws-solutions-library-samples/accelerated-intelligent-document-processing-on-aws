Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# S3 Module

The S3 module provides utilities for reading, writing, and listing objects in Amazon S3. It is used by virtually every other module in the IDP pipeline.

## Public Functions

| Function | Description |
|----------|-------------|
| `get_s3_client()` | Returns a lazily-initialized singleton boto3 S3 client |
| `get_text_content(s3_uri)` | Read text content from S3 (handles JSON `.text` field extraction) |
| `get_json_content(s3_uri)` | Read and parse JSON content from S3 |
| `get_binary_content(s3_uri)` | Read raw bytes from S3 |
| `write_content(content, bucket, key, content_type)` | Write string, bytes, dict, or list to S3 |
| `list_images_from_path(image_path)` | List image files from an S3 prefix or local directory |
| `find_matching_files(bucket, pattern, modified_after=None)` | Find S3 keys matching a glob pattern (`**` any depth, `*` one folder level, `?` one character; case-insensitive after the folder prefix) |

## Usage

### Reading Content

```python
from idp_common.s3 import get_text_content, get_json_content, get_binary_content

# Read text
text = get_text_content("s3://bucket/document/pages/1/result.json")

# Read JSON
data = get_json_content("s3://bucket/document/sections/1/result.json")

# Read binary
pdf_bytes = get_binary_content("s3://bucket/documents/sample.pdf")
```

### Writing Content

```python
from idp_common.s3 import write_content

# Write a dictionary as JSON
write_content(
    content={"status": "completed", "results": [...]},
    bucket="output-bucket",
    key="documents/doc-123/result.json",
    content_type="application/json"
)

# Write text
write_content(
    content="Processing complete",
    bucket="output-bucket",
    key="documents/doc-123/status.txt",
    content_type="text/plain"
)

# Write bytes
write_content(
    content=image_bytes,
    bucket="output-bucket",
    key="documents/doc-123/pages/1/image.jpg",
    content_type="image/jpeg"
)
```

### Listing Images

```python
from idp_common.s3 import list_images_from_path

# List images from S3 prefix
images = list_images_from_path("s3://config-bucket/examples/letters/")

# List images from local directory
images = list_images_from_path("/path/to/example-images/")
```

### Finding Files by Pattern

```python
from idp_common.s3 import find_matching_files

# Find all JSON files matching a pattern
files = find_matching_files(
    bucket="output-bucket",
    pattern="documents/*/sections/*/result.json"
)

# `**` matches any depth; only the literal folder prefix (documents/) is listed
files = find_matching_files(bucket="output-bucket", pattern="documents/**/result.json")
```

## S3 URI Format

All S3 functions accept URIs in the standard format: `s3://bucket-name/key/path`

The module parses these URIs internally to extract bucket and key components.
