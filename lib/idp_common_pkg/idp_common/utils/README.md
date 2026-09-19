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
| `sanitize_event_for_logging(event)` | Deep-copy an event with denylisted keys redacted and long content-field strings truncated, for safe `logger` output. ⚠️ **Has vendored copies — see below** |
| `scrub_jwts_in_string(text)` | Replace anything JWT-shaped in free-form text with `***REDACTED***` (not applied by `sanitize_event_for_logging`) |

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

One exception cuts the other way. `DETERMINISTIC_MESSAGE_MARKERS` lists message text
that marks a **reproducible** outcome even though the error *code* carrying it is
transient, and it is evaluated **first** for each node — ahead of the `ClientError`
code lookup, the exception-type check and the class-name lookup — so it beats the
verdict those would give. The only entry today is Bedrock's `Model produced invalid
sequence as part of ToolUse` (#895): `modelStreamErrorException` stays transient as a
code, because `ConverseStream` genuinely does break mid-stream for transport reasons,
but a model that emits a malformed `toolUse` block emits it again on attempt 8 (the
shard retrier's `MaxAttempts`). `is_model_tool_use_sequence_error(exc)` is the matching
predicate, used by `extraction/agentic_idp.py` to raise `ModelInvalidToolUseSequence`
with the model id and the remedies. Keep the tuple narrow: text that merely sounds deterministic
("invalid request", "unsupported") also appears inside genuinely transient wrappers.

```python
from idp_common.utils.transient_errors import raise_if_transient

try:
    ...
except Exception as e:
    raise_if_transient(e, where="extraction section 3")  # TransientError when transient
    raise  # hard errors keep their own name and are not retried
```

### The shard invocation's time budget (`bedrock_utils`)

Four module constants in `bedrock_utils` are only correct **relative to each other**,
so they are defined together rather than at the call sites that use them:

| Constant | Value | Bounds |
|---|---|---|
| `LAMBDA_MAX_TIMEOUT_SECONDS` | 900 | The shard function's `Timeout`, which is also Lambda's maximum |
| `AGENT_READ_TIMEOUT_SECONDS` | 180 | One Bedrock request stalling with no response, before botocore gives up |
| `AGENT_MAX_TOTAL_BACKOFF_SECONDS` | 300 | Total sleep the retry ladder may spend across all attempts |
| `AGENT_MAX_BACKOFF_SECONDS` | 60 | A single sleep |

The invariant is `AGENT_READ_TIMEOUT_SECONDS + AGENT_MAX_TOTAL_BACKOFF_SECONDS <
LAMBDA_MAX_TIMEOUT_SECONDS`, with room left over for the work itself: one stalled
request plus the whole backoff allowance must still leave the shard time to produce a
result. When it does not, the shard is killed by the Lambda timeout instead of
returning, Step Functions reports `Sandbox.Timedout`, and that is classified
**deterministic** with one attempt — so the transient failure a retry would have
cleared becomes the one that is not retried, and `ExtractionShardMap`, which tolerates
no shard failures, discards the sibling shards that had already succeeded
([#1014](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1014)).
This is the same shape as the `max_delay=1800`-inside-a-900-second-function error the
comments in that module describe, one layer down: in the boto3 client config rather
than the retry decorator.

They live here, not in `extraction.agentic_idp`, because `extraction.runtime` needs
them too and is deliberately importable without the strands-backed agentic stack.
`AGENT_READ_TIMEOUT_SECONDS` is the default of every `read_timeout` parameter on both
modules — a constant the callers do not read would satisfy the arithmetic and change
nothing.

`tests/unit/extraction/test_shard_timeout_budget.py` asserts the inequality, that both
modules take their default from the constant, that the deployed function's `Timeout`
still matches the assumed ceiling, and — on a simulated clock — that a shard meeting an
injected `ReadTimeoutError` returns it as a transient error with a full read-timeout
window still left to retry in.

Note what a reserve cannot promise. `clamp_sleep_to_budgets` keeps
`_DEADLINE_RESERVE_SECONDS` of the remaining budget unspent so a sleep does not end
exactly at the wall, but an agent call can legitimately take minutes, so the reserve is
a floor on usefulness rather than a guarantee that the next attempt completes. It also
only ever **shortens** a sleep: it never converts one into a failure, because being
killed by the Lambda timeout is retried by the caller while the underlying error name
is not.

### Parse LLM Responses

```python
from idp_common.utils import extract_structured_data_from_text

# Extract JSON or YAML from LLM response text
data, format_type = extract_structured_data_from_text(llm_response_text)
# data: parsed dict/list
# format_type: "json" or "yaml"
```

### Log redaction (`log_sanitizer`) — has vendored copies

```python
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

logger.info("Invoked with: %s", json.dumps(sanitize_event_for_logging(event)))
```

Returns a deep copy — the caller's object is never mutated — in which any key
whose name matches `_DEFAULT_DENY_KEY_SUBSTRINGS` (case-insensitive substring, at
any nesting depth) becomes `"***REDACTED***"`, and a string held directly under a
content-shaped key (`prompt`, `text`, `content`, `extracted_text`, …) is capped at
500 characters. It never raises: a non-dict input is returned unchanged, and an
object that cannot be deep-copied comes back as `"<uncopyable Foo>"`.

It is a **denylist**, so a newly added sensitive API field is not protected until
its name is added to `_DEFAULT_DENY_KEY_SUBSTRINGS` (or passed as
`extra_deny_keys=`). That is a deliberate trade: an allowlist would redact the
argument and field *names* operators need to read a log at all.

⚠️ **`log_sanitizer.py` is the one file in this package with committed copies
elsewhere in the repo, and editing it is not a one-file change.** Lambda functions
under both `nested/api-resolvers/src/lambda/` and `src/lambda/` carry no
`idp-common` layer. SAM packages each function from its own `CodeUri` directory, so
they can reach neither this library nor a sibling function's directory at runtime —
and attaching the base layer (Pillow, pypdfium2, requests: tens of MB) to a handful
of tiny resolvers and custom resources to reach a stdlib-only module is the wrong
trade. So each of them holds a **byte-identical** copy as `log_sanitizer.py` and
imports it as a top-level sibling module. The count is deliberately not quoted
here: it changes whenever a layer-free function starts logging its event, and the
authoritative answer is whatever `scripts/sync_resolver_log_sanitizer.sh` reports
on its last line.

The contract:

1. **Edit only this file.** Never edit a copy.
2. Then run **`scripts/sync_resolver_log_sanitizer.sh`**, which rewrites every
   copy. It derives its destinations from the handler sources — a directory is a
   destination because a file in it imports the sibling `log_sanitizer` module — so
   there is no target list to keep in step.
3. `scripts/tests/test_resolver_log_sanitizer.py` is a **blocking** test. It fails
   if any copy differs by one byte, if a function imports the sibling module
   without holding a copy (or holds an unused one), if a function imports the
   canonical module while its CloudFormation function declares no `IDPCommon*Layer`
   (an ImportError at cold start), if the sync script stops scanning the same
   Lambda trees the test does, if any handler hand-rolls a local key list again, or
   if any handler under either tree logs its whole invocation event without passing
   it through `sanitize_event_for_logging`.

Keep this file `ruff format`-clean and stdlib-only. Adding a third-party import
would break every layer-free copy at cold start, and a formatting difference
between this file and a copy breaks byte-identity.
