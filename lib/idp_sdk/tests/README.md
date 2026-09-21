# IDP SDK Tests

Comprehensive test suite for IDP SDK with unit and integration tests.

## Test Structure

```
tests/
├── conftest.py                          # Shared fixtures and pytest configuration
├── unit/                                # Unit tests (43 tests, fast, mocked)
│   ├── test_client.py                  # Client initialization and namespaces (15 tests)
│   ├── test_models.py                  # Pydantic models and enums (13 tests)
│   ├── test_exceptions.py              # Exception hierarchy (9 tests)
│   ├── test_batch_operations.py        # Batch operations mocked (3 tests)
│   ├── test_config_operations.py       # Config operations mocked (3 tests)
│   └── test_stack_operations.py        # Stack operations mocked (3 tests)
└── integration/                         # Integration tests (13 tests, real AWS)
    ├── test_stack_resources.py         # Stack resource operations (3 tests)
    ├── test_batch_processing.py        # Batch processing operations (3 tests)
    ├── test_document_processing.py     # Document operations (3 tests)
    └── test_config_management.py       # Config management operations (4 tests)
```

## Running Tests

### Unit Tests Only (CI/CD Safe - No AWS Required)
```bash
pytest -m "not integration"
```
This runs only unit tests (43 tests, fast, mocked, no AWS credentials needed).

### Integration Tests Only (Requires AWS)
```bash
export IDP_STACK_NAME=idp-stack-01
export AWS_REGION=us-east-1
pytest -m integration
```
**Note**: Integration tests require AWS credentials and deployed stack.

### All Tests (Unit + Integration)
```bash
export IDP_STACK_NAME=idp-stack-01
export AWS_REGION=us-east-1
pytest
```
**Warning**: This runs all 56 tests including integration tests that require AWS.

### By Domain
```bash
pytest -m stack        # Stack operations
pytest -m batch        # Batch operations
pytest -m document     # Document operations
pytest -m config       # Config operations
```

### Specific Test File
```bash
pytest tests/unit/test_client.py
pytest tests/integration/test_stack.py -v
```

## Test Markers

- `@pytest.mark.unit` - Fast unit tests with mocked dependencies (43 tests)
- `@pytest.mark.integration` - Integration tests requiring real AWS (13 tests)
- `@pytest.mark.stack` - Stack operation tests (6 tests: 3 unit + 3 integration)
- `@pytest.mark.batch` - Batch operation tests (6 tests: 3 unit + 3 integration)
- `@pytest.mark.document` - Document operation tests (3 integration tests)
- `@pytest.mark.config` - Config operation tests (7 tests: 3 unit + 4 integration)

## Requirements

### Unit Tests
- No AWS credentials required
- Fast execution (< 1 second)
- Mocked AWS services

### Integration Tests
- AWS credentials configured
- Deployed IDP stack
- Set environment variables:
  - `IDP_STACK_NAME` - Stack name
  - `AWS_REGION` - AWS region

## CI/CD Integration

### GitHub Actions Example
```yaml
# Run unit tests on every commit (no AWS required)
- name: Unit Tests
  run: pytest -m "not integration"

# Run integration tests manually or post-deployment
- name: Integration Tests
  if: github.event_name == 'workflow_dispatch'  # Manual trigger only
  env:
    IDP_STACK_NAME: IDP-Test-Stack
    AWS_REGION: us-east-1
  run: pytest -m integration
```

### Local Development
```bash
# Quick feedback (unit tests only, no AWS)
pytest -m "not integration"

# Full validation (requires AWS credentials and stack)
export IDP_STACK_NAME=idp-stack-01
export AWS_REGION=us-east-1
pytest
```

## Test Statistics

- **Total Tests**: 56 (43 unit + 13 integration)
- **Unit Tests**: 43 tests, ~0.5s execution time
- **Integration Tests**: 13 tests, ~45s execution time (requires AWS)

### Coverage

Generate coverage report:
```bash
pytest -m "not integration" --cov=idp_sdk --cov-report=html
open htmlcov/index.html
```

## Writing Tests

### Unit Test Example (Mocked)
```python
import pytest
from unittest.mock import Mock, patch
from idp_sdk import IDPClient
from idp_sdk.models import BatchListResult

@pytest.mark.unit
@pytest.mark.batch
class TestBatchOperationsMocked:
    @patch('idp_sdk.core.batch_processor.BatchProcessor')
    def test_list_batches(self, mock_processor):
        """Test listing batches with mocked AWS."""
        mock_instance = Mock()
        mock_instance.list_batches.return_value = {
            "batches": [{"batch_id": "batch-1", "document_ids": ["doc1"]}],
            "count": 1
        }
        mock_processor.return_value = mock_instance
        
        client = IDPClient(stack_name="test-stack")
        result = client.batch.list(limit=5)
        
        assert isinstance(result, BatchListResult)
        assert result.count == 1
```

### Integration Test Example (Real AWS)
```python
import pytest

@pytest.mark.integration
@pytest.mark.stack
class TestStackResources:
    def test_get_resources(self, client):
        """Test getting stack resources from real AWS."""
        resources = client.stack.get_resources()
        
        assert "InputBucket" in resources
        assert "OutputBucket" in resources
        assert "DocumentsTable" in resources
```

## Troubleshooting

### Integration Tests Fail
- Verify AWS credentials: `aws sts get-caller-identity`
- Check stack exists: `aws cloudformation describe-stacks --stack-name idp-stack-01`
- Verify environment variables are set

### Import Errors
Reinstall from the repository root, with `idp_common` on the same command line —
the SDK requires it by name, and a one-at-a-time install can resolve that name
from a public index instead of from `lib/`:

```bash
pip install -e lib/idp_common_pkg -e "lib/idp_sdk[dev]"
```
