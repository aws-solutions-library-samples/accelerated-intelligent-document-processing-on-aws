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
shard-runtime, assessment and rule-validation handlers therefore classify a failure
with `is_transient_error(exc)` and re-raise a transient cause as `TransientError`, the
one name `workflow.asl.json` lists for those eight tasks (#787, #1101). Rules: the exception's own
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

**Two entry points, and picking the wrong one fails silently.** `raise_if_transient`
returns without raising when the exception *already is* a `TransientError`, because it
is written for the block above — the bare `raise` is what keeps the name. An `except`
that instead **returns** a value has no such `raise`, so with `raise_if_transient`
alone an exception another site had already classified gets swallowed there and the
classification is undone. Use `reraise_if_transient` at any site that swallows:

```python
from idp_common.utils.transient_errors import reraise_if_transient

try:
    ...
except Exception as e:
    reraise_if_transient(e, where="rule validation rule 'must be employed'")
    return fallback_result  # reached only for a DETERMINISTIC failure
```

Neither ever wraps a `TransientError` in another one, so a failure that passes through
several nested handlers still reports one name and one cause. Rule validation is where
this matters most, because its blocks nest: a transient re-raised for one rule travels
up through `asyncio.gather` into a document-level `except` that returns a document
marked failed (#1101).

### The shard invocation's time budget (`idp_common.timeout_budget`)

The retry ladder in `bedrock_utils` spends part of a budget it does not own. The whole
of it lives in **`idp_common/timeout_budget.py`** — a leaf module that imports nothing,
for a reason given below — and its constants are only correct **relative to each
other**, so they are defined together rather than at the call sites that use them:

| Constant | Value | Bounds |
|---|---|---|
| `LAMBDA_MAX_TIMEOUT_SECONDS` | 900 | The shard function's `Timeout`, which is also Lambda's maximum |
| `AGENT_READ_TIMEOUT_SECONDS` | 180 | One socket read on the **streamed** agentic call — time to first event, then each inter-event gap |
| `CONFIDENCE_READ_TIMEOUT_SECONDS` | 300 | One **non-streamed** `converse` in `bedrock/client.py`, which bounds the whole response |
| `BOTOCORE_TOTAL_MAX_ATTEMPTS` | 1 | How many times botocore may attempt a call, and therefore the multiplier on both timeouts above |
| `AGENT_MAX_TOTAL_BACKOFF_SECONDS` | 90 | Total sleep the retry ladder may spend across all attempts |
| `AGENT_MAX_BACKOFF_SECONDS` | 60 | A single sleep |

The invariant is

```
BOTOCORE_TOTAL_MAX_ATTEMPTS * (AGENT_READ_TIMEOUT_SECONDS + CONFIDENCE_READ_TIMEOUT_SECONDS)
    + AGENT_MAX_TOTAL_BACKOFF_SECONDS
    + <room for the work>
<= LAMBDA_MAX_TIMEOUT_SECONDS
```

A shard invocation can stall on **two** Bedrock clients, not one. The streamed agentic
call is always there; the non-streamed one runs inside the same invocation whenever
confidence is in `separate` mode, because `ExtractionService._build_assess_runner` hands
`extract_one_shard` a closure over `AssessmentService.assess_results`. (In `integrated`
mode the extraction agent emits confidence inline, so only the streamed term applies —
but the budget has to hold for both.) 180 + 300 + 90 = 570 leaves 330 s for the work,
which is the floor the test enforces: enough for one complete call of the slowest kind.

When the inequality fails, the shard is killed by the Lambda timeout instead of
returning, Step Functions reports `Sandbox.Timedout`, and that is classified
**deterministic** with one attempt — so the transient failure a retry would have
cleared becomes the one that is not retried, and `ExtractionShardMap`, which tolerates
no shard failures, discards the sibling shards that had already succeeded
([#1014](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1014)).
This is the same shape as the `max_delay=1800`-inside-a-900-second-function error the
comments in that module describe, one layer down: in the boto3 client config rather
than the retry decorator.

⚠️ **`total_max_attempts`, never `max_attempts`.** In *client config* botocore's
`max_attempts` means max **retries** and is normalised to `total_max_attempts =
max_attempts + 1` (`botocore.args.ClientArgsCreator._compute_retry_max_attempts` says
so in its own comment). So `max_attempts=1` permits **two** attempts — one doubling of
the read timeout — and `max_attempts=7` permits **eight**. `total_max_attempts` passes
through verbatim and takes precedence, so it is the only spelling that means what it
says; the constant is named after that key on purpose. This matters because botocore
treats a read timeout as transient (`ReadTimeoutError` subclasses `HTTPClientError`,
which its `TransientRetryableChecker` lists) and retries it *inside the call*, where
neither the application ladder nor the deadline check can observe it.

**Why the constants are a leaf module and not part of this package.**
`settings_helper` builds an SSM client at *module scope*, so importing anything from
`idp_common.utils` requires a resolvable AWS region before any handler code runs. That
requirement is transitive: putting the budget here made `import idp_common.config`
need a region (via `merge_utils` → `idp_common.bedrock` → `bedrock.client`), and it
also broke `extraction/runtime.py`'s documented import-lightness — where the import
cannot be deferred, because it supplies a default argument, which is evaluated at
import time. `idp_common/timeout_budget.py` imports nothing, so it is free to be
imported from anywhere. `tests/unit/test_import_surface_region_free.py` holds that
property, in both directions.

`AGENT_READ_TIMEOUT_SECONDS` is the default of every `read_timeout` parameter in the
extraction modules — a constant the callers do not read would satisfy the arithmetic
and change nothing.

`tests/unit/extraction/test_shard_timeout_budget.py` asserts the whole inequality; that
both extraction modules take their default from the constant; that **both clients'
resolved botocore configurations** carry the budget's timeout and attempt count (read
off the live client, because a source scan cannot see botocore's normalisation); that
the deployed function's `Timeout` still matches the assumed ceiling; and — on a
simulated clock — that a shard meeting an injected `ReadTimeoutError` returns it as a
transient error with a full read-timeout window still left to retry in.

⚠️ **One exposure these constants do not bound.** `bedrock/client.py`'s
`_invoke_with_retry` has its own application-level ladder — `max_retries` attempts,
backing off `initial_backoff` doubling to `max_backoff` — and unlike
`invoke_agent_with_retry` it never calls `get_lambda_deadline_epoch`, so it can overrun
an invocation by itself whatever the arithmetic says. The inequality bounds one stalled
call per client, not that ladder. Making it deadline-aware changes behaviour for every
non-agentic path, so it is tracked separately; the test above pins its numbers and its
nominal worst case, and fails if either moves or if the ladder gains a deadline check
that makes the caveat obsolete.

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
