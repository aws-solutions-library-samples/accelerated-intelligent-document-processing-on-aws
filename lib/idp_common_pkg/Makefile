# Accept PYTHON from parent Makefile, or auto-detect
PYTHON ?= $(shell command -v python3 2>/dev/null || pyenv which python 2>/dev/null || echo python)

# HERMETIC_AWS / PYTEST_HERMETIC, shared with the repository-root Makefile so
# there is one definition of "the AWS environment a CI runner has". Resolved
# relative to THIS file rather than the working directory, so it works under
# `make -C lib/idp_common_pkg` (which is how CI invokes this) and from anywhere
# else. The unit targets below go through it; test-integration deliberately does
# not, because it needs the machine's real credentials and region.
include $(dir $(lastword $(MAKEFILE_LIST)))../../make/hermetic_aws.mk

.PHONY: test test-cicd clean

test: test-unit

# Run everything that isn't an integration test (requires AWS resources).
# NOT `-m "unit"`: hundreds of real unit tests were never marked, so the old
# `unit`-only filter silently skipped ~800+ of them. `not integration` matches
# pytest.ini's default and the rest of the repo's suites.
#
# The operator-run scripts that call real Bedrock live in manual_tests/ and are
# excluded by pytest.ini's `norecursedirs`, which covers a bare `pytest` too —
# these recipes no longer carry an --ignore of their own.
test-unit:
	$(PYTEST_HERMETIC) -m "not integration"

test-integration:
	$(PYTHON) -m pytest -m "integration"

test-cicd: test-unit-cicd

# SKIP_INSTALL=1 skips the editable reinstall — set it when the caller already
# installed idp_common[test] into the active environment (the CI code_checks
# job does, via uv in before_script). Avoids a redundant plain-pip resolve that
# re-downloads/recompiles heavy deps (e.g. numpy from source). Local `make
# test-cicd` leaves it unset and installs as before.
# PYTEST_ARGS lets CI pass `-n auto` (pytest-xdist) to parallelize across cores.
test-unit-cicd:
	@if [ -z "$(SKIP_INSTALL)" ]; then pip install -e ".[test]"; else echo "SKIP_INSTALL set — using pre-installed deps"; fi
	$(PYTEST_HERMETIC) -m "not integration" $(PYTEST_ARGS) --cov=idp_common --cov-report=xml:test-reports/coverage.xml --cov-report=term --junitxml=test-reports/test-results.xml tests/

clean:
	rm -rf .pytest_cache
	rm -rf .coverage
	rm -rf coverage.xml
	rm -rf test-results.xml
	rm -rf test-reports
	rm -rf htmlcov
	rm -rf dist
	rm -rf build
	rm -rf *.egg-info
	
# A convenience target to install the idp_common library with all, dev dependencies in edit mode
dev:
	pip install -e ".[all,dev]"
