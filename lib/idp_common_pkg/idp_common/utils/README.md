Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Utils Module

The Utils module provides common utility functions used across the IDP pipeline.

## Public Functions

| Function | Description |
|----------|-------------|
| `build_s3_uri(bucket, key)` | Construct an `s3://bucket/key` URI from components |
| `parse_s3_uri(s3_uri)` | Parse an S3 URI into `(bucket, key)` tuple |
| `merge_metering_data(existing, new)` | Merge token usage / metering dictionaries (sums numeric values) |
| `get_bedrock_region()` | Get the AWS region for Bedrock API calls |
| `extract_structured_data_from_text(text)` | Extract JSON or YAML structured data from LLM response text |

## Usage

### S3 URI Helpers

```python
from idp_common.utils import build_s3_uri, parse_s3_uri

# Build a URI
uri = build_s3_uri("my-bucket", "documents/sample.pdf")
# Returns: "s3://my-bucket/documents/sample.pdf"

# Parse a URI
bucket, key = parse_s3_uri("s3://my-bucket/documents/sample.pdf")
# Returns: ("my-bucket", "documents/sample.pdf")
```

### Metering Data

```python
from idp_common.utils import merge_metering_data

# Merge token usage from multiple LLM calls
combined = merge_metering_data(
    existing={"inputTokens": 100, "outputTokens": 50, "requests": 1},
    new={"inputTokens": 200, "outputTokens": 75, "requests": 1}
)
# Returns: {"inputTokens": 300, "outputTokens": 125, "requests": 2}
```

### Transient-error classification (`transient_errors`)

Step Functions retries a Lambda task by the reported error *name*. The extraction,
shard-runtime and assessment handlers therefore classify a failure with
`is_transient_error(exc)` and re-raise a transient cause as `TransientError`, the one
name `workflow.asl.json` lists for those five tasks (#787). Rules: the exception's own
verdict first (a `ClientError` is judged by its code alone — `ValidationException`
and `ModelErrorException` are deterministic at the task level, the Bedrock retry
codes, S3's `SlowDown` / `ServiceUnavailable` / `InternalError` and the stream error
are transient); botocore / urllib3 / stdlib network and timeout exception types are
transient; only explicit `raise ... from` links are followed (`__context__` is not, so
a retry loop's chained attempts or a swallowed transient error cannot make an
unrelated deterministic failure look transient); message markers are limited to
transport text (`Read timed out`, `Connection reset`, ...).

```python
from idp_common.utils.transient_errors import raise_if_transient

try:
    ...
except Exception as e:
    raise_if_transient(e, where="extraction section 3")  # TransientError when transient
    raise  # hard errors keep their own name and are not retried
```

### Parse LLM Responses

```python
from idp_common.utils import extract_structured_data_from_text

# Extract JSON or YAML from LLM response text
data, format_type = extract_structured_data_from_text(llm_response_text)
# data: parsed dict/list
# format_type: "json" or "yaml"
```
