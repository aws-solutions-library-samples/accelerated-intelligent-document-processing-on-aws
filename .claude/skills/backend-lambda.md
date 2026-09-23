# Backend Lambda Development — GenAI IDP Accelerator

## Stack & Runtime
- **Python**: 3.12 (target: `>=3.12,<3.14`)
- **Framework**: AWS SAM (`AWS::Serverless-2016-10-31`)
- **Packaging**: Zip (utility Lambdas) or Container Image via ECR (processing Lambdas)
- **Architecture**: `arm64` preferred
- **Linting**: ruff (line-length 88, double quotes, isort, TID251 banned-api rules)
- **Type Checking**: basedpyright (basic mode, Python 3.12, Linux platform)
- **Testing**: pytest with `@pytest.mark.unit` / `@pytest.mark.integration` markers

## Lambda File Structure
Every Lambda function lives in its own directory under `src/lambda/`:
```
src/lambda/<function_name>/
├── index.py            # ALWAYS named index.py
├── requirements.txt    # Function-specific deps
└── __pycache__/
```

## Lambda Handler Template
ALWAYS follow this pattern for new Lambda functions:
```python
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import json
import os
import logging
from typing import Any
from aws_xray_sdk.core import xray_recorder, patch_all

patch_all()

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Module-level AWS client initialization (connection reuse across invocations)
dynamodb = boto3.resource("dynamodb")

# Use TYPE_CHECKING guard for type stubs that aren't available at runtime
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table
else:
    Table = object


@xray_recorder.capture("function_name")
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Brief description of what this handler does."""
    ...
```

## Key Patterns (MUST follow)
1. **Handler**: Always `handler(event, context)` in `index.py` → SAM references as `Handler: index.handler`
2. **Logging**: `logging.getLogger()` at module level; level from `LOG_LEVEL` env var
3. **X-Ray**: Import and call `patch_all()` at module level
4. **AWS clients**: Initialize at module level (outside handler) for connection reuse
5. **Environment variables**: `os.environ['REQUIRED']` for required, `os.environ.get('OPTIONAL', 'default')` for optional
6. **Error handling**: Catch `botocore.exceptions.ClientError` explicitly; use `logger.error(..., exc_info=True)` for stack traces
7. **License header**: First two lines of EVERY file must be the MIT-0 copyright + SPDX header

## Return Patterns
- **SQS batch processor**: `{"batchItemFailures": [{"itemIdentifier": id}]}`
- **API-style**: `{"statusCode": 200|400|500, "body": message}`

## idp_common Library (`lib/idp_common_pkg/`)
The shared library powering all processing. Key modules:
- `idp_common.models` — `Document`, `Page`, `Section`, `Status` (Pydantic v2)
- `idp_common.docs_service` — Factory: `create_document_service()`
- `idp_common.config` — `ConfigurationManager` for DynamoDB-backed config
- `idp_common.bedrock` — Bedrock FM client with caching and guardrails
- `idp_common.extraction` — Data extraction (traditional + agentic modes)
- `idp_common.classification` — Document classification
- `idp_common.ocr` — OCR (Textract + Bedrock backends)
- `idp_common.evaluation` — Evaluation with stickler-eval
- `idp_common.agents` — Strands agent integration

**Modular installation** — only install what the Lambda needs:
```
pip install -e "lib/idp_common_pkg[core]"         # Minimal
pip install -e "lib/idp_common_pkg[ocr]"          # OCR support
pip install -e "lib/idp_common_pkg[extraction]"   # Extraction
pip install -e "lib/idp_common_pkg[all]"          # Everything
```

**Lazy loading**: `idp_common/__init__.py` uses `__getattr__` for lazy submodule imports. Don't import submodules eagerly at package level.

## Banned Imports
- `from idp_sdk._core import ...` — BANNED. Use `from idp_sdk import IDPClient` instead.
- This is enforced by ruff rule `TID251`.

## Key Dependencies
- `boto3==1.42.0`, `pydantic>=2.12.0`, `jsonschema>=4.25.1`
- `strands-agents==1.14.0` (agent framework)
- `bedrock-agentcore>=0.1.1`
- `PyYAML>=6.0.0`, `deepdiff>=6.0.0`

## Ruff Configuration Highlights
```toml
line-length = 88
target-version = "py312"
select = ["E4", "E7", "E9", "F"]
extend-select = ["I", "TID251"]   # isort + banned API
quote-style = "double"
```
Note: every tracked `.py` file is linted, including any file you add. What `ruff.toml` excludes is a named per-file list of pre-existing debt (`[lint] exclude`, `[format] exclude`), generated from `scripts/lint_debt.json` and ratcheted by `make check-lint-debt` — never add to it.

## Pyright Configuration Highlights
- `typeCheckingMode`: `basic` (not strict)
- `reportUndefinedVariable`: `error`
- `reportReturnType`: `warning`
- `reportCallIssue`: `warning`
- Most other checks are relaxed (`none`) — this is intentional for gradual adoption

## Adding a New Lambda to the SAM Template
When adding a new Lambda to `patterns/unified/template.yaml`:
1. Use `AWS::Serverless::Function` resource type
2. Include `PermissionsBoundary` conditional: `!If [HasPermissionsBoundary, !Ref PermissionsBoundaryArn, !Ref AWS::NoValue]`
3. Add security suppression metadata for cfn-nag (W89, W92) and checkov (CKV_AWS_116, CKV_AWS_117, CKV_AWS_115)
4. Create a dedicated `AWS::Logs::LogGroup` with KMS encryption and configurable retention
5. Pass standard env vars: `LOG_LEVEL`, `METRIC_NAMESPACE`, `STACK_NAME`
6. Use `!Sub "arn:${AWS::Partition}:..."` for ALL ARNs (GovCloud compatibility)
7. Use `!Sub "...${AWS::URLSuffix}"` instead of hardcoded `amazonaws.com`

## Commands
```bash
make ruff-lint          # Lint with auto-fix
make format             # Format Python code
make typecheck          # basedpyright type checking
make test               # Run all tests
cd lib/idp_common_pkg && make test-unit    # Unit tests only
cd lib/idp_common_pkg && make dev          # Install in edit mode
```
