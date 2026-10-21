---
paths:
  - "src/lambda/**"
  - "lib/**/*.py"
---

# Backend Lambda rules

Applies to Lambda handlers under `src/lambda/` and to Python library code under
`lib/`, above all `idp_common`. Template wiring for a new Lambda is in
[infrastructure.md](infrastructure.md).

## Must
- Put each Lambda in its own `src/lambda/<function_name>/` directory, with an
  `index.py` that exposes `handler(event, context)` (SAM `Handler: index.handler`)
  and its own `requirements.txt`.
- Start every Python file with the MIT-0 header:
  `# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.` followed by
  `# SPDX-License-Identifier: MIT-0`.
- Keep handlers thin. A handler parses the event, calls a service function, and
  formats the response. Business logic, data access and transformations go in
  `idp_common`, or in a service module next to the handler.
- **Reuse `idp_common` before you write anything new.** Check `models` (`Document`,
  `Page`, `Section`, `Status`), `config`, `bedrock`, `s3`, `dynamodb`, `utils`,
  `hooks`, `agents`, `extraction`, `classification` and `ocr` first. If a function
  is nearly right, give it a parameter or a subclass instead of copying it. If a
  second Lambda needs the same logic, move it into `idp_common`.
- Create AWS clients at module scope so warm invocations reuse them. Call
  `logging.getLogger()` with its level taken from `LOG_LEVEL`, and call X-Ray
  `patch_all()` at module level.
- Read required environment variables with `os.environ["NAME"]` and optional
  ones with `os.environ.get("NAME", default)`.
- Catch `botocore.exceptions.ClientError` explicitly. Log real faults with
  `logger.error(..., exc_info=True)`. Log authorization denials at WARNING, as
  set out in [api-authorization.md](api-authorization.md).
- When a transient cause reaches a handler as an ordinary Python exception,
  re-raise it under `TransientError` using `idp_common.utils.transient_errors`.
  Step Functions retriers match on the exception's class name.
- Use the established return shapes: `{"batchItemFailures": [...]}` for SQS
  batch processors and `{"statusCode": ..., "body": ...}` for API-style handlers.
- A filtered DynamoDB `Scan` must paginate on `LastEvaluatedKey` and project only
  the attributes it needs. A deliberately bounded sample carries
  `# filtered-scan-ok: <reason>`.
- A new `idp_common` subpackage must register for lazy loading in
  `idp_common/__init__.py` (`__getattr__`) and get its own optional-dependency
  extra in `pyproject.toml`.
- In a Lambda's `requirements.txt`, name only the extras it uses, by relative
  path (for example `../../lib/idp_common_pkg[extraction]`).
- When you change one of `chat_with_document_processor` and its vendored copy
  under `src/lambda/chat_stream_processor/vendored/`, change the other in the
  same commit.
- When `idp_common` behaviour changes, update the module README for that
  subpackage as well (see [documentation.md](documentation.md)).

## Must not
- Import from `idp_sdk._core`. Use `from idp_sdk import IDPClient`. Ruff `TID251`
  enforces this.
- Copy `idp_common` modules into a Lambda directory, or reach into another
  package with a `sys.path` hack.
- Call raw `boto3` where an `idp_common` wrapper already exists for that service.
  In particular, never create a `bedrock-runtime` client outside `idp_common.bedrock`.
- Hardcode model IDs, prompts, class labels, account IDs or regions. Read them
  from configuration (see [config-and-models.md](config-and-models.md)).
- Use `print()` for logging, write a bare `except:`, or swallow an exception
  without logging it.
- Re-wrap a domain exception as a generic `Exception`. Doing so loses the class
  name, which retriers and the API dispatcher depend on.

## Verify with
- `make ruff-lint`, `make format` and `make typecheck`
- `make check-filtered-scans`
- `cd lib/idp_common_pkg && make test-unit`, then `make test`

## Sources
- [.claude/skills/backend-lambda.md](../skills/backend-lambda.md): the handler
  template, ruff and pyright settings, and the lazy-loading and mock-patch details
- [.claude/skills/code-review.md](../skills/code-review.md): the Lambda and
  DynamoDB review checklist
- [lib/idp_common_pkg/idp_common/README.md](../../lib/idp_common_pkg/idp_common/README.md):
  the library layout and lazy submodule loading
- [docs/configuration.md](../../docs/configuration.md): how transient errors are
  classified and how Step Functions retries them
