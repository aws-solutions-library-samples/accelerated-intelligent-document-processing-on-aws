# Accept PYTHON from parent Makefile, or auto-detect
PYTHON ?= $(shell command -v python3 2>/dev/null || pyenv which python 2>/dev/null || echo python)

# HERMETIC_AWS / PYTEST_HERMETIC, shared with the repository-root Makefile so
# there is one definition of "the AWS environment a CI runner has". Resolved
# relative to THIS file rather than the working directory, so it works under
# `make -C lib/idp_common_pkg` (which is how CI invokes this) and from anywhere
# else. The unit targets below go through it; test-integration deliberately does
# not, because it needs the machine's real credentials and region.
include $(dir $(lastword $(MAKEFILE_LIST)))../../make/hermetic_aws.mk

# Distribute this suite across cores. `pytest-xdist` has been a declared `[test]`
# dependency since it was added "For parallel test execution", but no target ever
# passed `-n`, so every run was serial.
#
# Measured on a 16-core host, same tree, full suite with coverage:
#
#     serial    8306 passed, TOTAL 79%   in 584s
#     -n auto   8306 passed, TOTAL 79%   in  94s     (6.2x)
#
# Identical pass count, identical coverage total, identical uncovered-statement
# count -- which is also a useful statement about the suite: no test in it depends
# on execution order, or distribution across workers would have changed a result.
#
# Overridable, and worth overriding in two cases: `PYTEST_PARALLEL=` restores serial
# execution for a debugging run, because xdist interleaves worker output and breaks
# `-s`, `--pdb` and live logging. `-n <N>` pins the worker count where `auto` reads
# the host's core count but the container's CPU quota is lower.
#
# NOT applied to test-integration: those tests share real AWS resources, so
# concurrent workers would race on them rather than merely run faster.
PYTEST_PARALLEL ?= -n auto

# Aggregate coverage floor. The blunt half of the ratchet: it catches a general slide,
# and it cannot see one module regressing while another grows -- which is what
# scripts/check_coverage_debt.py's per-file baseline is for. Both halves exist because
# neither subsumes the other.
#
# Set one point below the figure measured when the baseline was recorded, so ordinary
# run-to-run variation does not red-line a branch nobody changed. Raise it when the
# baseline is re-recorded upward. `COV_FLOOR=` disables it, which is what you want when
# running a subset of the suite -- a partial run legitimately covers less.
COV_FLOOR ?= --cov-fail-under=82

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
	$(PYTEST_HERMETIC) $(PYTEST_PARALLEL) -m "not integration"

test-integration:
	$(PYTHON) -m pytest -m "integration"

test-cicd: test-unit-cicd

# SKIP_INSTALL=1 skips the editable reinstall — set it when the caller already
# installed idp_common[test] into the active environment (the CI code_checks
# job does, via uv in before_script). Avoids a redundant plain-pip resolve that
# re-downloads/recompiles heavy deps (e.g. numpy from source). Local `make
# test-cicd` leaves it unset and installs as before.
# PYTEST_ARGS passes extra flags through. It must NOT be used to pass `-n`, which is
# PYTEST_PARALLEL's job: two `-n` on one command line make pytest collect nothing and
# exit 5. GitLab used to pass `-n auto` here, which is why the parallel default lives
# in this file now -- as a CI-side override it left GitHub running the suite serially.
test-unit-cicd:
	@if [ -z "$(SKIP_INSTALL)" ]; then pip install -e ".[test]"; else echo "SKIP_INSTALL set — using pre-installed deps"; fi
	$(PYTEST_HERMETIC) $(PYTEST_PARALLEL) -m "not integration" $(PYTEST_ARGS) $(COV_FLOOR) --cov=idp_common --cov-report=xml:test-reports/coverage.xml --cov-report=term --junitxml=test-reports/test-results.xml tests/

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
