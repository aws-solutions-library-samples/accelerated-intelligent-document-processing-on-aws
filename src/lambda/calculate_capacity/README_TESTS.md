# Capacity Planning Lambda Tests

This directory contains unit tests for the capacity planning Lambda functions.

## Running Tests

### Quick Start (Recommended)

The capacity planning tests are integrated into the main project test suite:

```bash
# From project root - install all dependencies (including test dependencies)
make setup

# Run all project tests (includes capacity planning)
make test

# Run only capacity planning tests
make test-capacity

# Run capacity planning tests with coverage
make test-capacity-coverage
```

### Manual Test Execution

If you prefer to run tests manually:

#### Install Test Dependencies

```bash
cd src/lambda/calculate_capacity
pip install -r requirements-test.txt
```

#### Run All Tests

```bash
# Run all tests with verbose output
pytest -v

# Run with coverage report
pytest --cov=. --cov-report=html --cov-report=term

# Run specific test file
pytest test_validation.py -v

# Run specific test class
pytest test_validation.py::TestEnvironmentValidation -v

# Run specific test
pytest test_validation.py::TestEnvironmentValidation::test_validate_all_required_vars_present -v
```

### View Coverage Report

After running tests with coverage, open the HTML report:

```bash
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
start htmlcov/index.html  # Windows
```

## Test Organization

### `test_validation.py`
Tests for input validation and environment variable validation:
- Environment variable validation
- JSON input sanitization
- Capacity input parameter validation
- Caching of validated environment variables

### `test_capacity_calculations.py`
Tests for core capacity calculation logic:
- Latency distribution calculations
- Quota requirement calculations
- Decimal conversion utilities
- Recommendation generation

## Test Coverage Goals

Target: **80%+ code coverage** for critical functions

Priority areas:
1. ✅ Input validation (100% coverage)
2. ✅ Environment variable validation (100% coverage)
3. ✅ Quota calculations (80%+ coverage)
4. ✅ Latency calculations (80%+ coverage)
5. Decimal conversion (100% coverage)
6. Recommendation generation (60%+ coverage)

## Adding New Tests

When adding new functionality:

1. Write tests first (TDD approach recommended)
2. Ensure tests cover:
   - Happy path
   - Error cases
   - Edge cases
   - Input validation
3. Maintain or improve coverage percentage
4. Update this README if new test files are added

## CI/CD Integration

Both CI systems reach this suite through `make test-packages-cicd`, which names this
directory explicitly:

```
cd src/lambda/calculate_capacity && $(PYTEST_HERMETIC) -q -p no:cacheprovider
```

`$(PYTEST_HERMETIC)` runs pytest with the AWS environment stripped — no region, no
credentials, no profile — because that is what a CI runner has. A suite that needs a
region or placeholder credentials supplies them from its own `conftest.py`.

`make test` runs this suite too, via `scripts/run_all_tests.py`'s auto-discovery, and
is the command to use locally. It is not what CI runs: neither `.gitlab-ci.yml` nor
any workflow in `.github/workflows/` invokes it. `scripts/tests/test_src_lambda_tests_in_ci.py`
is what keeps the two in agreement, by failing when a directory `make test` discovers
is reachable from neither CI-invoked target. For a local coverage run, use
`make test-capacity-coverage` (above).

## Mock Environment Variables

Tests use `patch.dict(os.environ, ...)` to mock environment variables.

Required environment variables for tests:
- `TRACKING_TABLE`
- `METERING_TABLE_NAME`
- `LAMBDA_MEMORY_GB`
- `BEDROCK_MODEL_QUOTA_CODES`
- `BEDROCK_MODEL_RPM_QUOTA_CODES`
- All threshold environment variables

See `test_validation.py` for complete list.

## Troubleshooting

### Import Errors

If you see import errors, ensure you're running tests from the correct directory:

```bash
cd src/lambda/calculate_capacity
python -m pytest
```

### Missing Dependencies

Install all test dependencies:

```bash
pip install -r requirements-test.txt
```

### AWS Credential Errors

Tests mock AWS services using `moto`. If you see credential errors, ensure moto is installed:

```bash
pip install moto>=4.2.0
```
