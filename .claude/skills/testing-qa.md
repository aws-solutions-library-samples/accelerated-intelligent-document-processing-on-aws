# Testing & QA — GenAI IDP Accelerator

## Test Framework
- **Framework**: pytest
- **Markers**: `@pytest.mark.unit`, `@pytest.mark.integration`
- **Default**: Unit tests only (`addopts = -m "not integration"`)
- **AWS Mocking**: `moto` (`@mock_aws` decorator)
- **Mocking**: `unittest.mock` (`MagicMock`, `patch`)
- **Coverage**: `pytest-cov` for coverage reports

## Test Locations
| Package | Test Directory | Command |
|---------|---------------|---------|
| `idp_common` | `lib/idp_common_pkg/tests/` | `cd lib/idp_common_pkg && make test` |
| `idp_cli` | `lib/idp_cli_pkg/tests/` | `cd lib/idp_cli_pkg && pytest -v` |
| `idp_sdk` | `lib/idp_sdk/tests/` | `cd lib/idp_sdk && pytest -m "not integration" -v` |
| Capacity Lambda | `src/lambda/calculate_capacity/` | `make test-capacity` |
| Config Library | `config_library/test_config_library.py` | `make test-config-library` |

## Test File Structure
Tests mirror the module structure:
```
tests/
├── conftest.py              # Global setup (AWS creds, module mocks)
├── unit/
│   ├── agents/              # Mirrors idp_common/agents/
│   ├── assessment/          # Mirrors idp_common/assessment/
│   ├── classification/      # Mirrors idp_common/classification/
│   ├── config/              # Mirrors idp_common/config/
│   ├── extraction/          # Mirrors idp_common/extraction/
│   ├── ocr/                 # Mirrors idp_common/ocr/
│   └── ...                  # 50+ test files
├── integration/             # Integration tests (require AWS)
└── resources/               # Test data / fixtures
```

## conftest.py Pattern
The global conftest.py does critical setup:
```python
import os, sys
from unittest.mock import MagicMock

# Set AWS credentials BEFORE any boto3 imports
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Mock heavy external dependencies that aren't needed in unit tests
sys.modules["strands"] = MagicMock()
sys.modules["strands.agent"] = MagicMock()
sys.modules["bedrock_agentcore"] = MagicMock()
# ... more module mocks
```

## Writing Unit Tests
### Class-Based Pattern (preferred)
```python
import pytest
import boto3
from moto import mock_aws
from idp_common.models import Document, Status

class TestMyFeature:
    def setup_method(self):
        """Set up test fixtures."""
        self.document = Document(
            id="test-doc-123",
            input_bucket="input-bucket",
            input_key="test.pdf",
            status=Status.QUEUED,
        )
        self.bucket = "test-working-bucket"

    @pytest.mark.unit
    @mock_aws
    def test_happy_path(self):
        """Test the normal processing flow."""
        s3_client = boto3.client("s3", region_name="us-east-1")
        s3_client.create_bucket(Bucket=self.bucket)
        # ... test logic
        assert result["status"] == "success"

    @pytest.mark.unit
    def test_error_handling(self):
        """Test error cases."""
        with pytest.raises(ValueError, match="Invalid document"):
            process_document(None)
```

### Function-Based Tests
```python
@pytest.mark.unit
def test_specific_function():
    result = my_function(input_data)
    assert result == expected
```

## Moto Usage
Always use `@mock_aws` decorator:
```python
from moto import mock_aws

@mock_aws
def test_s3_operation():
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="test-bucket")
    # ... operations happen against moto mock
```

## Local Lambda Testing
```bash
cd patterns/unified/
sam build
sam local invoke OCRFunction -e ../../testing/OCRFunction-event.json --env-vars ../../testing/env.json
```

## Sample Documents
Available in `samples/` directory for testing document processing.

## Evaluation Framework
For document processing accuracy:
- Baseline data in S3 Evaluation Baseline bucket
- Per-field evaluation methods: `EXACT`, `NUMERIC_EXACT`, `FUZZY`
- `stickler-eval` library for metrics
- Run via CLI: `idp-cli run-evaluation --stack-name <name>`

## Test Studio (Web UI)
Interactive testing via the web UI — upload documents, compare results across config versions.

## Commands Reference
```bash
make test                    # Every non-integration suite, auto-discovered
make test-integration-all    # Every integration-marked suite (needs AWS)
make test-list               # Show discovered roots (run vs quarantined)
make test-cli                # CLI tests only
make test-config-library     # Config validation only
make test-capacity           # Capacity planning tests
make test-capacity-coverage  # With coverage report

# Direct pytest invocations
cd lib/idp_common_pkg
make test-unit               # Unit tests only
make test-integration        # Integration tests (needs AWS)
pytest -m "unit" -k "test_extraction"   # Filter by name
pytest -v --tb=short         # Verbose with short tracebacks
pytest --cov=idp_common --cov-report=html   # Coverage report
```

## Writing a gate exemption

Turning a gate off for anything means registering it in
`scripts/tests/gate_exemptions.json`; `test_gate_exemption_registry.py` fails on an
unregistered exemption list and names it. The full rules are in CLAUDE.md
("Every gate exemption is registered"), but the four that decide most reviews:

1. **One entry per file, ideally per line.** A reason bound to a directory answers for
   every file under it, and an aggregate reading of it passes even when it is false of
   most of them. That is the exact shape of four shipped defects.
2. **Compute the premise if you can.** `scripts/tests/gate_premises.py` holds the
   predicates (`not_a_nested_stack_of_parent`, `built_separately_from_main_stack`,
   `file_absent_or_untracked`, `installer_manifest_pins_parameter`). Each takes **one**
   member — parametrise over your members rather than asking whether the reason holds
   generally.
3. **`JUDGEMENT` is allowed, with a written reason.** It says there is nothing to
   compute; it does not say nobody looked.
4. **Give it a ratchet**, or declare the gap in `ratchetGap`. Non-vacuity (it must
   shield something today), count pinning (it shields only as many sites as were
   audited), universe closure (nothing may sit outside both sets), staleness.

Two patterns worth copying rather than reinventing:
`lib/idp_common_pkg/tests/unit/bedrock/test_long_context_metering_key.py` stores
`(reason, site count)` so a new site in an exempt file still fails, and
`scripts/tests/test_log_group_encryption.py::test_every_log_group_template_is_categorised`
derives its universe and fails if any member is in no category — which is why its
categories can be trusted, and how it found eleven log groups a hand-built inventory
missed.

## How `make test` finds every suite (scripts/run_all_tests.py)
The repo's Python tests live in ~30 separate roots (packages + per-Lambda dirs).
A single `pytest` from the repo root FAILS: the many `tests/conftest.py` files
all import as the module `tests.conftest` and collide
(`ImportPathMismatchError`; `--import-mode=importlib` hits a duplicate-plugin
error too). So `make test` runs `scripts/run_all_tests.py`, which runs each root
as its own isolated pytest invocation.

The script DISCOVERS every directory containing `test_*.py` and checks it against
two registries in the file: `RUN_ROOTS` (run in the gate) and `QUARANTINE`
(excluded, each with a reason). **A discovered dir in neither list is a hard
error** — so when you add tests in a NEW location, `make test`/CI fails until you
add that dir to `RUN_ROOTS` (if green headless) or `QUARANTINE` (with a reason).
This is deliberate: it's the guard that stops new tests from being silently
skipped, which is exactly how ~200 Lambda tests went unrun under the old
hand-maintained `make test`. Currently quarantined roots (need fixing before
they join the gate): `ocr_benchmark_deployer` (needs `huggingface_hub`, not a test
dependency), `s3_vectors_manager` (one stale assertion in `test_handler.py`; the
other four tests pass), `scripts` (the RBAC harness, not a suite), the
`chandra-ocr-hook` manual script, and the `idp_sdk/_core` source tree. Run
`make test-list` to see the current split, and read the reason beside each entry in
`QUARANTINE` rather than this list — the reasons are what `scripts/tests/test_run_all_tests_registry.py`
computes against the tree.

**Being registered in `RUN_ROOTS` does not mean a suite runs on a pull request.**
`make test` runs in neither CI. CI runs `make test-cicd -C lib/idp_common_pkg` and
`make test-packages-cicd`, and the second is a hand-enumerated recipe — so a new root
has to be added there as well, on its own `cd <dir> && $(PYTEST_HERMETIC) …` line if it
defines a module named `index`. `scripts/tests/test_src_lambda_tests_in_ci.py` derives
both sides and fails until you do; 22 roots holding 506 tests were in `RUN_ROOTS` and
in neither CI before it was generalised beyond `src/lambda/`. Everything gated goes
through `$(PYTEST_HERMETIC)`, which strips the AWS environment, so a suite that needs a
region or placeholder credentials supplies them from its own `conftest.py` with
`os.environ.setdefault`.
