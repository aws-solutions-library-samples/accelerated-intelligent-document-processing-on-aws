# Makefile for code quality and formatting
#
# Run 'make help' to see all available targets.

# Define color codes
RED := \033[0;31m
GREEN := \033[0;32m
YELLOW := \033[1;33m
CYAN := \033[0;36m
BOLD := \033[1m
NC := \033[0m  # No Color

# Virtual environment configuration
VENV_DIR := .venv
# Use the venv python/pip if the venv exists, otherwise fall back to system
ifeq ($(wildcard $(VENV_DIR)/bin/python),)
  PYTHON := $(shell command -v python3 2>/dev/null || pyenv which python 2>/dev/null || echo python)
  PIP := $(shell command -v pip3 2>/dev/null || echo pip)
else
  PYTHON := $(CURDIR)/$(VENV_DIR)/bin/python
  PIP := $(CURDIR)/$(VENV_DIR)/bin/pip
endif

# idp-cli invocation — uses `python -m idp_cli.cli` so it works whether or not
# the virtualenv is activated (picks up $(PYTHON) which prefers .venv).
IDP_CLI := $(PYTHON) -m idp_cli.cli
# Extension-author CLI (idp_feature_sdk). Used by the seller-side targets.
IDP_FEATURE_CLI := $(PYTHON) -m idp_feature_sdk.cli

# First-party packages that live in THIS repo and are NOT published to PyPI.
#
# SECURITY — dependency confusion: these packages depend on each other by bare
# name (idp_cli_pkg -> "idp-sdk", idp_sdk -> "idp_common"), and those names are
# squatted by third parties on public PyPI. If they are installed one pip
# invocation at a time, pip resolves a not-yet-installed sibling from PyPI and
# silently pulls in the squatted package. Installing them all in a SINGLE pip
# invocation lets pip satisfy those names from the local checkout instead.
# Keep this list complete, and never split it across multiple pip calls.
FIRST_PARTY_EDITABLES := \
	-e "lib/idp_common_pkg[all,dev,test]" \
	-e lib/idp_sdk \
	-e lib/idp_cli_pkg \
	-e lib/idp_mcp_connector_pkg \
	-e lib/idp_feature_sdk

##@ General
.PHONY: help
help: ## Show this help message
	@echo ""
	@echo "Usage: make [target]"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"; section=""} \
		/^##@/ { section=substr($$0, 5); next } \
		/^[a-zA-Z_-]+:.*?## / { \
			if (section != "" && section != last_section) { \
				printf "\n  \033[1m%s\033[0m\n", section; \
				last_section = section \
			}; \
			printf "  \033[36m%-25s\033[0m %s\n", $$1, $$2 \
		}' $(MAKEFILE_LIST)
	@echo ""

# Default target - run both lint and test
.DEFAULT_GOAL := all
all: lint test ## Run lint + test (default)

##@ Setup
.PHONY: install-first-party
install-first-party: ## Install ALL first-party packages in ONE pip pass (CI: make install-first-party PIP="uv pip")
	@# ONE invocation, deliberately — see the dependency-confusion note on
	@# FIRST_PARTY_EDITABLES above. Splitting this lets pip resolve a
	@# not-yet-installed sibling ("idp-sdk", "idp_common") from public PyPI, where
	@# those names are squatted by third parties.
	@#
	@# PIP is overridable because CI builds its venv with `uv venv`, which does not
	@# install pip into it; CI passes PIP="uv pip".
	$(PIP) install $(FIRST_PARTY_EDITABLES)
	@# Fails if any first-party package resolved from PyPI rather than the checkout.
	$(PYTHON) scripts/check_first_party_deps.py

setup: ## Install all packages into current Python environment (no venv)
	@# Always use the current shell's pip, ignoring .venv even if it exists
	@SETUP_PIP=$$(python3 -m pip --version >/dev/null 2>&1 && echo "python3 -m pip" || echo "pip3"); \
	SETUP_PYTHON=$$(command -v python3 2>/dev/null || echo python); \
	echo "Installing packages into current Python environment..."; \
	echo "Python: $$($$SETUP_PYTHON --version) at $$(which $$SETUP_PYTHON)"; \
	echo "Pip: $$SETUP_PIP"; \
	echo ""; \
	echo "Upgrading pip..."; \
	$$SETUP_PIP install --upgrade pip && \
	echo "Installing all first-party packages (single resolution pass)..." && \
	$$SETUP_PIP install $(FIRST_PARTY_EDITABLES) && \
	echo "Verifying first-party packages resolved locally (not from PyPI)..." && \
	$$SETUP_PYTHON scripts/check_first_party_deps.py && \
	echo "Installing capacity planning test dependencies..." && \
	$$SETUP_PIP install -r src/lambda/calculate_capacity/requirements-test.txt && \
	echo "Installing cfn-lint for CloudFormation template validation..." && \
	$$SETUP_PIP install cfn-lint && \
	echo "" && \
	echo -e "$(GREEN)✅ Setup complete! idp_common, idp-cli, idp_sdk, idp_mcp_connector, idp_feature_sdk, and test dependencies are now installed.$(NC)" && \
	echo -e "$(YELLOW)   Tip: Use 'make setup-venv' instead to install into an isolated virtual environment.$(NC)"

setup-venv: ## Create .venv and install all packages into it
	@echo "Creating virtual environment in $(VENV_DIR)..."
	@PYENV_PYTHON=$$(pyenv which python 2>/dev/null); \
	SYS_PYTHON=$$(command -v python3 2>/dev/null); \
	BASE_PYTHON=$${PYENV_PYTHON:-$$SYS_PYTHON}; \
	if [ -z "$$BASE_PYTHON" ]; then \
		echo -e "$(RED)ERROR: No python3 or pyenv python found. Install Python 3.12+ first.$(NC)"; \
		exit 1; \
	fi; \
	echo "Using base Python: $$BASE_PYTHON ($$($$BASE_PYTHON --version))"; \
	$$BASE_PYTHON -m venv $(VENV_DIR)
	@echo "Upgrading pip..."
	$(VENV_DIR)/bin/pip install --upgrade pip
	@echo "Installing all first-party packages (single resolution pass)..."
	$(VENV_DIR)/bin/pip install $(FIRST_PARTY_EDITABLES)
	@echo "Verifying first-party packages resolved locally (not from PyPI)..."
	$(VENV_DIR)/bin/python scripts/check_first_party_deps.py
	@echo "Installing capacity planning test dependencies..."
	$(VENV_DIR)/bin/pip install -r src/lambda/calculate_capacity/requirements-test.txt
	@echo "Installing cfn-lint for CloudFormation template validation..."
	$(VENV_DIR)/bin/pip install cfn-lint
	@echo ""
	@echo -e "$(GREEN)✅ Setup complete! Virtual environment created at $(VENV_DIR)$(NC)"
	@echo -e "$(GREEN)   idp_common, idp-cli, idp_sdk, idp_mcp_connector, idp_feature_sdk, and test dependencies are now installed.$(NC)"
	@echo -e "$(YELLOW)   All 'make' targets will automatically use $(VENV_DIR)/bin/python.$(NC)"
	@echo -e "$(YELLOW)   To activate manually: source $(VENV_DIR)/bin/activate$(NC)"

##@ Code Quality
lint: ruff-lint format check-arn-partitions check-filtered-scans check-data-plane-tags validate-buildspec ui-lint codegen-check ## Run all linting (ruff, format, ARN checks, filtered scans, buildspec, UI, codegen). Use FORCE=1 to force UI lint re-run despite checksum match.
fastlint: ruff-lint format check-arn-partitions check-filtered-scans check-data-plane-tags validate-buildspec ## Quick lint without UI checks

ruff-lint: ## Run ruff linting with auto-fix
	ruff check --fix

format: ## Format Python code with ruff
	ruff format

lint-cicd: ## CI/CD lint — checks only, no modifications
	@echo "Running code quality checks..."
	@if ! ruff check; then \
		echo -e "$(RED)ERROR: Ruff linting failed!$(NC)"; \
		echo -e "$(YELLOW)Please run 'make ruff-lint' locally to fix these issues.$(NC)"; \
		exit 1; \
	fi
	@if ! ruff format --check; then \
		echo -e "$(RED)ERROR: Code formatting check failed!$(NC)"; \
		echo -e "$(YELLOW)Please run 'make format' locally to fix these issues.$(NC)"; \
		exit 1; \
	fi; \
	echo "All checks passed!"
	@echo "Frontend checks"
	@if ! make ui-lint; then \
		echo -e "$(RED)ERROR: UI lint failed$(NC)"; \
		exit 1; \
	fi

	@# ui-build-only (vite build, NO lint/typecheck) — ui-lint above already ran
	@# eslint + tsc, so the default `ui-build` would redundantly run them again.
	@if ! make ui-build-only; then \
		echo -e "$(RED)ERROR: UI build failed$(NC)"; \
		exit 1; \
	fi

	@if ! make codegen-check; then \
		echo -e "$(RED)ERROR: GraphQL codegen check failed$(NC)"; \
		exit 1; \
	fi

	@echo "GovCloud ARN partition check"
	@if ! make check-arn-partitions; then \
		echo -e "$(RED)ERROR: Hardcoded ARN partitions/service principals found (breaks GovCloud)$(NC)"; \
		exit 1; \
	fi

	@echo "DynamoDB filtered-scan pagination check"
	@if ! make check-filtered-scans; then \
		echo -e "$(RED)ERROR: Filtered DynamoDB scan(s) cannot see all their matches (see issue #599)$(NC)"; \
		exit 1; \
	fi

	@echo "Data-plane Lambda tag check"
	@if ! make check-data-plane-tags; then \
		echo -e "$(RED)ERROR: Data-plane Lambda tag check failed (see docs/reporting-sql-layer.md §10.3)$(NC)"; \
		exit 1; \
	fi

	@echo -e "$(GREEN)All code quality checks passed!$(NC)"

check-filtered-scans: ## Check for DynamoDB filtered Scans that can't see all matches (issue #599)
	@$(PYTHON) scripts/check_filtered_scans.py || \
		(echo -e "$(RED)ERROR: Unpaginated filtered DynamoDB scan(s) found!$(NC)" && exit 1)

check-data-plane-tags: ## Enforce idp:plane=data on the whitelisted data-plane Lambdas (see docs/reporting-sql-layer.md §10.3)
	@$(PYTHON) scripts/check_data_plane_tags.py || \
		(echo -e "$(RED)ERROR: Data-plane Lambda tag check failed!$(NC)" && exit 1)

validate-buildspec: ## Validate AWS CodeBuild buildspec files
	@echo "Validating buildspec files..."
	@$(PYTHON) scripts/sdlc/validate_buildspec.py patterns/*/buildspec.yml || \
		(echo -e "$(RED)ERROR: Buildspec validation failed!$(NC)" && exit 1)
	@echo -e "$(GREEN)✅ All buildspec files are valid!$(NC)"

check-arn-partitions: ## Check CloudFormation templates for hardcoded ARN partitions
	@echo "Checking CloudFormation templates for hardcoded ARN partitions and service principals..."
	@FOUND_ISSUES=0; \
	for template in template.yaml patterns/*/template.yaml patterns/*/sagemaker_classifier_endpoint.yaml options/*/template.yaml feature-platform/*/template.yaml; do \
		if [ -f "$$template" ]; then \
			echo "Checking $$template..."; \
			ARN_MATCHES=$$(grep -n "arn:aws:" "$$template" | grep -v "arn:\$${AWS::Partition}:" || true); \
			if [ -n "$$ARN_MATCHES" ]; then \
				echo -e "$(RED)ERROR: Found hardcoded 'arn:aws:' references in $$template:$(NC)"; \
				echo "$$ARN_MATCHES" | sed 's/^/  /'; \
				echo -e "$(YELLOW)  These should use 'arn:\$${AWS::Partition}:' instead for GovCloud compatibility$(NC)"; \
				FOUND_ISSUES=1; \
			fi; \
			SERVICE_MATCHES=$$(grep -n "\.amazonaws\.com" "$$template" | grep -v "\$${AWS::URLSuffix}" | grep -v "^[0-9]*:[[:space:]]*#" | grep -v "Description:" | grep -v "Comment:" | grep -v "cognito" | grep -v "ContentSecurityPolicy" || true); \
			if [ -n "$$SERVICE_MATCHES" ]; then \
				echo -e "$(RED)ERROR: Found hardcoded service principal references in $$template:$(NC)"; \
				echo "$$SERVICE_MATCHES" | sed 's/^/  /'; \
				echo -e "$(YELLOW)  These should use '\$${AWS::URLSuffix}' instead of 'amazonaws.com' for GovCloud compatibility$(NC)"; \
				echo -e "$(YELLOW)  Example: 'lambda.amazonaws.com' should be 'lambda.\$${AWS::URLSuffix}'$(NC)"; \
				FOUND_ISSUES=1; \
			fi; \
			CONSOLE_MATCHES=$$(grep -n "console\.aws\.amazon\.com\|s3\.console\.aws\.amazon\.com" "$$template" | grep -v "^[0-9]*:[[:space:]]*#" | grep -v "Domain:" | grep -v "Description:" | grep -v "Comment:" || true); \
			if [ -n "$$CONSOLE_MATCHES" ]; then \
				echo -e "$(RED)ERROR: Found hardcoded AWS console domain references in $$template:$(NC)"; \
				echo "$$CONSOLE_MATCHES" | sed 's/^/  /'; \
				echo -e "$(YELLOW)  Console URLs must be partition-aware for GovCloud (console.amazonaws-us-gov.com).$(NC)"; \
				echo -e "$(YELLOW)  Use !FindInMap [ConsoleDomainMap, !Ref \"AWS::Partition\", Domain] and the$(NC)"; \
				echo -e "$(YELLOW)  regional host form 'https://\$${AWS::Region}.\$${ConsoleDomain}/...' (works for S3 too).$(NC)"; \
				FOUND_ISSUES=1; \
			fi; \
		fi; \
	done; \
	for asl in patterns/*/statemachine/*.asl.json options/*/statemachine/*.asl.json feature-platform/*/statemachine/*.asl.json; do \
		if [ -f "$$asl" ]; then \
			echo "Checking $$asl..."; \
			ASL_MATCHES=$$(grep -n "arn:aws:" "$$asl" | grep -v "arn:\$${Partition}:" || true); \
			if [ -n "$$ASL_MATCHES" ]; then \
				echo -e "$(RED)ERROR: Found hardcoded 'arn:aws:' references in $$asl:$(NC)"; \
				echo "$$ASL_MATCHES" | sed 's/^/  /'; \
				echo -e "$(YELLOW)  State-machine ASL uses DefinitionSubstitutions, so these should use$(NC)"; \
				echo -e "$(YELLOW)  'arn:\$${Partition}:' (add 'Partition: !Ref AWS::Partition' to$(NC)"; \
				echo -e "$(YELLOW)  DefinitionSubstitutions). Hardcoded 'aws' breaks Step Functions in GovCloud.$(NC)"; \
				FOUND_ISSUES=1; \
			fi; \
		fi; \
	done; \
	if [ $$FOUND_ISSUES -eq 0 ]; then \
		echo -e "$(GREEN)✅ No hardcoded ARN partition or service principal references found!$(NC)"; \
	else \
		echo -e "$(RED)❌ Found hardcoded references that need to be fixed for GovCloud compatibility$(NC)"; \
		exit 1; \
	fi
	@# The loops above cover CloudFormation templates and Step Functions ASL only.
	@# Python was never scanned, which is how a hardcoded arn:aws: reached runtime
	@# and broke every Bedrock Data Automation invoke in GovCloud (issue #527).
	@$(PYTHON) scripts/check_python_arn_partitions.py

##@ Type Checking
typecheck: ## Run type checks with basedpyright
	@echo "Running type checks..."
	basedpyright

typecheck-stats: ## Type checks with detailed statistics
	@echo "Running type checks with statistics..."
	basedpyright --stats

# Usage: make typecheck-pr [TARGET_BRANCH=branch_name]
TARGET_BRANCH ?= develop
typecheck-pr: ## Type check only files changed vs TARGET_BRANCH (default: main)
	@echo "Type checking changed files against $(TARGET_BRANCH)..."
	$(PYTHON) scripts/sdlc/typecheck_pr_changes.py $(TARGET_BRANCH)

##@ Tests — pytest suites (offline unless noted)
# Three tiers of tests in this repo, split across two groups so it's obvious
# what each needs:
#   1. Offline pytest (no AWS)          → this group (`test`, `test-*`,
#      `api-test-static`). Safe in CI / on any machine.
#   2. Integration pytest (hits AWS)    → this group, marked "(requires AWS)"
#      (`test-integration-all`). Uses live AWS but deploys NO stack.
#   3. Stack tests (deploy/validate a   → the "Stack tests" group below
#      full IDP stack; heavy, manual)     (`stacktest-*`, `api-test`).
#
# The repo's Python tests live in ~30 separate roots (packages + per-Lambda
# dirs), each with its own conftest/mini-environment — a single `pytest` from
# the repo root fails because the many `tests/conftest.py` files collide. So
# `scripts/run_all_tests.py` DISCOVERS every test directory and runs each as an
# isolated pytest invocation. It also fails if it finds a test dir that isn't
# registered (RUN or QUARANTINE), so new tests can never be silently skipped —
# the gap that let the old hand-maintained list here miss ~200 Lambda tests.
test: ## Run every non-integration test suite (auto-discovered; see scripts/run_all_tests.py)
	$(PYTHON) scripts/run_all_tests.py

test-integration-all: ## Run every integration-marked suite across all roots (requires AWS)
	$(PYTHON) scripts/run_all_tests.py --integration

test-list: ## List the discovered test roots (run vs quarantined) without running them
	$(PYTHON) scripts/run_all_tests.py --list

test-packages-cicd: ## CI-safe: run the package/Lambda suites NOT covered by idp_common_pkg test-cicd (all green headless, no AWS)
	@echo "Running idp_cli_pkg tests..."
	cd lib/idp_cli_pkg && $(PYTHON) -m pytest -q -p no:cacheprovider
	@echo "Running idp_sdk tests (not integration)..."
	cd lib/idp_sdk && $(PYTHON) -m pytest -m "not integration" -q -p no:cacheprovider
	@echo "Running idp_feature_sdk tests..."
	cd lib/idp_feature_sdk && $(PYTHON) -m pytest -q -p no:cacheprovider
	@echo "Running feature platform tests..."
	cd feature-platform/main-stack-extensions && $(PYTHON) -m pytest -q -p no:cacheprovider
	cd feature-platform/feature-template/feature-api && $(PYTHON) -m pytest -q -p no:cacheprovider
	@echo "Running seller entitlement service tests (incl. template-security + payload fuzz)..."
	cd feature-platform/seller-entitlement-service && $(PYTHON) -m pytest tests -q -p no:cacheprovider
	@echo "Running capacity planning Lambda tests..."
	cd src/lambda/calculate_capacity && $(PYTHON) -m pytest -q -p no:cacheprovider
	@echo "Running circuit breaker Lambda tests..."
	$(PYTHON) -m pytest -q -p no:cacheprovider \
	    src/lambda/circuit_breaker_manager \
	    src/lambda/queue_processor/test_check_circuit_breaker.py \
	    src/lambda/workflow_tracker/test_notify_circuit_breaker.py
	@echo "Running queue_sender Lambda tests (folder-skip + #719 re-upload cleanup)..."
	@# Both suites import their own ``index`` module; run each in its
	@# own directory to prevent the sys.path collision that fails a
	@# combined pytest invocation.
	cd src/lambda/queue_sender && $(PYTHON) -m pytest test_index.py -q -p no:cacheprovider
	cd nested/api-resolvers/src/lambda/reprocess_document_resolver && $(PYTHON) -m pytest test_delete_output_data.py -q -p no:cacheprovider
	@echo "Running Chat-with-Document Lambda tests..."
	$(PYTHON) -m pytest -q -p no:cacheprovider \
	    src/lambda/chat_with_document_processor/tests \
	    nested/api-resolvers/src/lambda/send_chat_document_message_resolver/tests
	@echo "Running Chat-stream processor tests (incl. vendored-in-sync guard)..."
	cd src/lambda/chat_stream_processor && $(PYTHON) -m pytest tests -q -p no:cacheprovider
	@echo "Running BDA OCR project custom-resource tests (incl. library drift guard)..."
	cd src/lambda/bda_ocr_project && $(PYTHON) -m pytest tests -q -p no:cacheprovider
	@echo "Running fine-tuning job creator tests (ARN partition passthrough)..."
	cd src/lambda/finetuning_job_creator && $(PYTHON) -m pytest tests -q -p no:cacheprovider
	@echo "Validating config library files..."
	$(PYTHON) -m pytest config_library/test_config_library.py -q -p no:cacheprovider
	@echo "Running SDLC harness tests (incl. IAM trust-policy partition guards)..."
	$(PYTHON) -m pytest scripts/sdlc/tests -q -p no:cacheprovider
	@echo "Running repo-script tests (Python ARN-partition gate)..."
	$(PYTHON) -m pytest scripts/tests -q -p no:cacheprovider
	@echo "Running SRT gate tests (CI-visibility split + suppression baseline hygiene)..."
	$(PYTHON) -m pytest scripts/srt/tests -q -p no:cacheprovider
	@echo "Running dependency-audit gate tests (OSV allowlist + .ash.yaml hygiene)..."
	$(PYTHON) -m pytest scripts/security/tests -q -p no:cacheprovider
	@echo -e "$(GREEN)✅ All package/Lambda CI suites passed!$(NC)"

test-cli: ## Run only IDP CLI tests
	@echo "Running IDP CLI tests..."
	cd lib/idp_cli_pkg && $(PYTHON) -m pytest -v
	@echo -e "$(GREEN)✅ All CLI tests passed!$(NC)"

test-config-library: ## Run only config library validation tests
	@echo "Validating config library YAML/JSON files..."
	$(PYTHON) -m pytest config_library/test_config_library.py -v

test-capacity: ## Run only capacity planning tests
	@echo "Running capacity planning Lambda tests..."
	cd src/lambda/calculate_capacity && $(PYTHON) -m pytest -v

test-capacity-coverage: ## Run capacity planning tests with coverage report
	@echo "Running capacity planning Lambda tests with coverage..."
	cd src/lambda/calculate_capacity && $(PYTHON) -m pytest --cov=. --cov-report=term --cov-report=html -v
	@echo -e "$(GREEN)✅ Coverage report generated at src/lambda/calculate_capacity/htmlcov/index.html$(NC)"

test-circuit-breaker: ## Run only circuit breaker tests
	@echo "Running circuit breaker Lambda tests..."
	$(PYTHON) -m pytest -v \
	    src/lambda/circuit_breaker_manager \
	    src/lambda/queue_processor/test_check_circuit_breaker.py \
	    src/lambda/workflow_tracker/test_notify_circuit_breaker.py

# api-test-static is the OFFLINE half (tier 1, CI-safe) — it stays in this
# pytest group. The full api-test (tier 3, needs a live stack) lives in the
# "Stack tests" group below, alongside its `stacktest-rbac` alias.
api-test-static: ## Static RBAC/authorization scan of all API operations (no AWS; CI-safe)
	@echo "Running static API RBAC scan..."
	$(PYTHON) scripts/sdlc/scan_api_rbac.py $(if $(STRICT),--strict,)

##@ Stack tests (stacktest-*: run against / deploy a live stack, manual)
# One family for every test that exercises a REAL deployed stack (as opposed to
# the offline unit suites under `make test`). These run OUTSIDE the CI pipeline
# — on a dev box with AWS creds (AWS_PROFILE=default or idp-ci) — so heavy,
# concurrent, or infra-variant tests don't burst the account-wide control planes
# the way running them all at once in one pipeline did.
#
# The deploy-variant stack-tests (APIGateway hosting, WAF, PRIVATE/VPC, Jobs API,
# ZAP DAST) in particular no longer run automatically in CI; run them here on
# demand, each on its own stack. Two modes:
#   * STACK_NAME=<existing>  → validate that already-deployed stack (fast)
#   * omit STACK_NAME        → self-deploy a throwaway stack + validate + teardown
#     (needs TEMPLATE_URL from publish.py).
# VPC stack-tests (jobsapi, apigwpriv) take VPC wiring as make params:
#   VPC_ID=... SUBNET_IDS=a,b LAMBDA_SG_ID=... APIGW_VPCE_ID=...
# (falls back to IDP_TEST_* env vars; the run-stack-tests skill can
# discover a suitable VPC and fill these in for you).
_STACKTEST_VPC_ARGS = $(if $(VPC_ID),--vpc-id $(VPC_ID),) $(if $(SUBNET_IDS),--subnet-ids $(SUBNET_IDS),) $(if $(LAMBDA_SG_ID),--lambda-sg-id $(LAMBDA_SG_ID),) $(if $(APIGW_VPCE_ID),--apigw-vpce-id $(APIGW_VPCE_ID),)
_STACKTEST_ARGS = $(if $(STACK_NAME),--stack-name $(STACK_NAME),) $(if $(REGION),--region $(REGION),) $(if $(TEMPLATE_URL),--template-url $(TEMPLATE_URL),) $(if $(ADMIN_EMAIL),--admin-email $(ADMIN_EMAIL),) $(_STACKTEST_VPC_ARGS)

stacktest-list: ## List the available deploy-variant stack-tests
	$(PYTHON) scripts/sdlc/run_stacktest.py --list

# Usage: make api-test STACK_NAME=<stack-name> [REGION=<region>] [REPORT_DIR=<dir>] [NO_TEARDOWN=1]
# Full RBAC test: runs the offline static scan (api-test-static) first, then
# dynamic tests against the DEPLOYED stack — creates temporary Cognito users (one
# per group + a config-version-scoped Author), exercises every API op across all
# roles + unauthenticated + token negatives, and tears the test users down after.
# Requires AWS creds (see CLAUDE.md — use AWS_PROFILE=default).
api-test: api-test-static ## Full RBAC test: static scan + live API tests (requires STACK_NAME)
ifndef STACK_NAME
	$(error STACK_NAME is not set. Usage: make api-test STACK_NAME=<stack-name> [REGION=...])
endif
	@echo "Running dynamic API RBAC tests against stack $(STACK_NAME)..."
	$(PYTHON) scripts/test_api_rbac.py \
	    --stack-name $(STACK_NAME) \
	    $(if $(REGION),--region $(REGION),) \
	    --report-dir $(if $(REPORT_DIR),$(REPORT_DIR),./scratch/api-test-results) \
	    $(if $(NO_TEARDOWN),--no-teardown,)
	@echo -e "$(GREEN)✅ API RBAC report written to $(if $(REPORT_DIR),$(REPORT_DIR),./scratch/api-test-results)$(NC)"

# Alias so the RBAC test shows up under the consistent stacktest-* name too.
stacktest-rbac: api-test ## RBAC/API authorization test (alias: api-test) — needs STACK_NAME

# Usage: make ux-test STACK_NAME=<stack-name> [REGION=<region>] [GROUP=Admin]
# Browser-driven UX test. Not a self-contained target on purpose: the browsing and
# the usability judgement are done by the agent following
# .claude/skills/ux-test.md, so this prepares a throwaway session and prints what
# to do next rather than pretending a shell script can assess a user experience.
ux-test: ## Set up a browser UX-test session (requires STACK_NAME; see .claude/skills/ux-test.md)
ifndef STACK_NAME
	$(error STACK_NAME is not set. Usage: make ux-test STACK_NAME=<stack-name> [REGION=... GROUP=...])
endif
	@echo "Creating a throwaway UX-test session for $(STACK_NAME)..."
	@$(PYTHON) scripts/ux_test_session.py setup $(STACK_NAME) \
	    --group $(if $(GROUP),$(GROUP),Admin) \
	    $(if $(REGION),--region $(REGION),)
	@echo -e "$(YELLOW)Now drive the flows in scripts/ux_flows.yaml — see .claude/skills/ux-test.md$(NC)"
	@echo -e "$(YELLOW)Remember to run the teardown command printed above.$(NC)"

# Reports default under ./scratch (gitignored) so a manual run never litters the
# working tree; override the location with REPORT_DIR=.
stacktest-zap: ## ZAP DAST scan (STACK_NAME=... [REPORT_DIR=./dir] or self-deploy w/ TEMPLATE_URL=...)
	IDP_ZAP_REPORT_DIR=$(if $(REPORT_DIR),$(REPORT_DIR),./scratch/zap-reports) $(PYTHON) scripts/sdlc/run_stacktest.py zapdast $(_STACKTEST_ARGS)

stacktest-hosting-global: ## APIGateway GLOBAL hosting variant
	$(PYTHON) scripts/sdlc/run_stacktest.py apigw $(_STACKTEST_ARGS)

stacktest-waf: ## WAF-enabled hosting variant
	$(PYTHON) scripts/sdlc/run_stacktest.py waf $(_STACKTEST_ARGS)

stacktest-hosting-private: ## APIGateway PRIVATE (VPC) hosting variant (needs VPC_ID=...)
	$(PYTHON) scripts/sdlc/run_stacktest.py apigwpriv $(_STACKTEST_ARGS)

stacktest-jobsapi: ## Jobs API (VPC) variant (needs VPC_ID=...)
	$(PYTHON) scripts/sdlc/run_stacktest.py jobsapi $(_STACKTEST_ARGS)

# --- Template-TRANSFORM deploy tests (--headless / --govcloud) ---------------
# These do NOT go through run_stacktest.py: every probe there deploys the
# STANDARD template with different parameters, whereas these deploy the
# TRANSFORMED template via the documented user path (idp-cli deploy --headless /
# --govcloud --from-code .) and then process a real sample document. That is the
# only tier that can prove a transform produces a DEPLOYABLE stack — the gap that
# shipped issues #676, #677 and the SuppressAdminInvite dangling parameter.
# Each run is a full publish + deploy (~1h+). Not wired into CI; see
# .claude/skills/transform-deploy-test.md.
_TRANSFORM_ARGS = $(if $(REGION),--region $(REGION),) \
	$(if $(ADMIN_EMAIL),--admin-email $(ADMIN_EMAIL),) \
	$(if $(STACK_NAME),--stack-name $(STACK_NAME),) \
	$(if $(KEEP),--keep,) $(if $(SKIP_DOC_TEST),--skip-doc-test,) \
	$(if $(JSON_OUT),--json-out $(JSON_OUT),)

transform-deploy-test-list: ## List the transform deploy-tests
	$(PYTHON) scripts/sdlc/transform_deploy_test.py --list

transform-deploy-test-headless: ## Deploy a REAL --headless stack + process a sample doc
	$(PYTHON) scripts/sdlc/transform_deploy_test.py headless $(_TRANSFORM_ARGS)

transform-deploy-test-govcloud: ## Deploy a REAL --govcloud stack + process a sample doc (REGION=us-gov-west-1 for a true GovCloud run)
	$(PYTHON) scripts/sdlc/transform_deploy_test.py govcloud $(_TRANSFORM_ARGS)

transform-deploy-test-all: ## Both transform deploy-tests, one after the other
	$(PYTHON) scripts/sdlc/transform_deploy_test.py both $(_TRANSFORM_ARGS)

# Seller Entitlement Service e2e. Not a main-stack variant, so it does not go
# through run_stacktest.py: it deploys its own standalone stack (into a seller
# account) and tears it down again.
#
# This is the guard for deploy-time defects, which the offline suites structurally
# cannot see. Both real failures in this service so far — SAM's CALLER_CREDENTIALS
# default conflicting with the resource policy, and the missing account-level API
# Gateway CloudWatch role — were caught only by attempting a deploy.
#
# Needs no real Marketplace listing: every assertion is a refusal, so it registers
# a synthetic product id and skips the ownership check.
stacktest-seller: ## Seller Entitlement Service e2e: deploy throwaway stack, probe live API, teardown
	feature-platform/seller-entitlement-service/tests/stacktest.sh \
	    $(if $(STACK_NAME),--stack-name $(STACK_NAME)) \
	    $(if $(REGION),--region $(REGION)) \
	    $(if $(NO_TEARDOWN),--no-teardown)

# Release-vs-release benchmark audit (alias to benchmark-release).
stacktest-benchmark: benchmark-release ## Release benchmark audit (alias: benchmark-release)

# In-place stack upgrade validation (X→Y). No standalone harness in-repo yet —
# see .claude/skills/test-upgrade.md for the procedure.
stacktest-upgrade: ## Show how to run the in-place upgrade test (see test-upgrade skill)
	@echo "Upgrade testing is documented in .claude/skills/test-upgrade.md"
	@echo "It deploys a FROM release, update-stacks to a TO release, and watches"
	@echo "the UpdateDefaultConfig custom resource. Follow that skill's steps."

##@ UI Development
# Usage: make ui-start STACK_NAME=<stack-name>
ui-start: ## Start UI dev server (requires STACK_NAME for .env generation)
ifndef STACK_NAME
	$(error STACK_NAME is not set. Usage: make ui-start STACK_NAME)
endif
	@if [ -n "$(STACK_NAME)" ]; then \
		echo "Retrieving .env configuration from stack $(STACK_NAME)..."; \
		ENV_CONTENT=$$(aws cloudformation describe-stacks \
			--stack-name $(STACK_NAME) \
			--query "Stacks[0].Outputs[?OutputKey=='WebUITestEnvFile'].OutputValue" \
			--output text 2>/dev/null); \
		if [ -z "$$ENV_CONTENT" ] || [ "$$ENV_CONTENT" = "None" ]; then \
			echo -e "$(RED)ERROR: Could not retrieve WebUITestEnvFile from stack $(STACK_NAME)$(NC)"; \
			echo -e "$(YELLOW)Make sure the stack exists and has completed deployment.$(NC)"; \
			exit 1; \
		fi; \
		echo "$$ENV_CONTENT" > src/ui/.env; \
		echo -e "$(GREEN)✅ Created src/ui/.env from stack outputs$(NC)"; \
	fi
	@if [ ! -f src/ui/.env ]; then \
		echo -e "$(RED)ERROR: src/ui/.env not found$(NC)"; \
		echo -e "$(YELLOW)Either provide STACK_NAME to auto-generate, or create .env manually.$(NC)"; \
		echo -e "$(YELLOW)Usage: make ui-start STACK_NAME=<your-stack-name>$(NC)"; \
		exit 1; \
	fi
	@echo "Installing UI dependencies..."
	cd src/ui && npm ci --prefer-offline --no-audit
	@echo "Starting UI development server..."
	cd src/ui && npm run start

# `npm ci` wipes and reinstalls node_modules (~1 min). The UI targets below each
# ran it, so a single `make lint-cicd` did it 3× (ui-lint + ui-build-only +
# codegen-check). Set SKIP_NPM_CI=1 when the caller has ALREADY installed UI
# deps (the CI code_checks job installs once in before_script) to make these a
# no-op; unset (local) installs as before.
NPM_CI := $(if $(SKIP_NPM_CI),true,npm ci --prefer-offline --no-audit)

ui-lint: ## Run UI linting with checksum caching (skips if unchanged). Use FORCE=1 to force re-run.
	@echo "Checking if UI lint is needed..."
	@CURRENT_HASH=$$($(PYTHON) -c "from publish import IDPPublisher; p = IDPPublisher(); print(p.get_directory_checksum('src/ui'))"); \
	STORED_HASH=$$(test -f src/ui/.checksum && cat src/ui/.checksum || echo ""); \
	if [ -n "$(FORCE)" ] || [ "$$CURRENT_HASH" != "$$STORED_HASH" ]; then \
		if [ -n "$(FORCE)" ]; then \
			echo "FORCE=1 set - running lint..."; \
		else \
			echo "UI code checksum changed - running lint..."; \
		fi; \
		cd src/ui && $(NPM_CI) && npm run lint -- --fix && npm run typecheck || exit 1; \
		echo "$$CURRENT_HASH" > .checksum; \
		echo -e "$(GREEN)✅ UI lint and typecheck completed and checksum updated$(NC)"; \
	else \
		echo -e "$(GREEN)✅ UI code checksum unchanged - skipping lint (use FORCE=1 to force re-run)$(NC)"; \
	fi

ui-build: ## Build UI for production (runs lint + typecheck + vite build)
	@echo "Checking UI build"
	cd src/ui && $(NPM_CI) && npm run build

ui-build-only: ## Vite production build ONLY (no lint/typecheck) — for CI, where ui-lint already ran them
	@echo "Building UI (vite only; lint+typecheck already done by ui-lint)"
	cd src/ui && $(NPM_CI) && npm run build:only

ui-test: ## Run UI unit tests (Vitest, jsdom — no browser required)
	@echo "Running UI unit tests..."
	cd src/ui && npm ci --prefer-offline --no-audit && npx vitest run

##@ Code Generation
codegen: ## Regenerate GraphQL types and operations
	@cd src/ui && npm run codegen
	@echo -e "$(GREEN)✅ GraphQL types regenerated. Don't forget to commit the changes.$(NC)"

codegen-check: ## Verify GraphQL codegen output is up-to-date
	@echo "Checking if GraphQL codegen output is up-to-date..."
	@cd src/ui && $(NPM_CI) && npm run codegen
	@if ! git diff --quiet src/ui/src/graphql/generated/; then \
		if [ -n "$$CI" ] || [ -n "$$GITHUB_ACTIONS" ]; then \
			echo -e "$(RED)ERROR: Generated GraphQL files are out of date!$(NC)"; \
			echo -e "$(YELLOW)Run 'make codegen' and commit the updated files.$(NC)"; \
			git --no-pager diff --stat src/ui/src/graphql/generated/; \
			exit 1; \
		else \
			echo -e "$(YELLOW)Generated GraphQL files were out of date — auto-updated.$(NC)"; \
			git --no-pager diff --stat src/ui/src/graphql/generated/; \
			echo -e "$(YELLOW)Please commit the changes above.$(NC)"; \
		fi \
	else \
		echo -e "$(GREEN)✅ GraphQL codegen output is up-to-date$(NC)"; \
	fi

classes-from-bda: ## Generate standard class catalog from BDA blueprints
	@echo "Generating standard class catalog from BDA standard blueprints..."
	$(PYTHON) scripts/generate_standard_classes.py --region us-east-1 --output src/ui/src/data/standard-classes.json
	@echo -e "$(GREEN)✅ Standard class catalog updated! Review changes in src/ui/src/data/standard-classes.json$(NC)"

##@ Git Workflow
commit: lint test ## Lint, test, auto-generate commit message, commit, and push
	@echo "Generating commit message via Bedrock..."
	@git add . && \
	COMMIT_MESSAGE=$$(bash scripts/generate_commit_message.sh) && \
	echo "Commit message: $$COMMIT_MESSAGE" && \
	git commit -m "$$COMMIT_MESSAGE" && \
	git push

fastcommit: fastlint ## Fast lint only, auto-generate commit message, commit, and push
	@echo "Generating commit message via Bedrock..."
	@git add . && \
	COMMIT_MESSAGE=$$(bash scripts/generate_commit_message.sh) && \
	echo "Commit message: $$COMMIT_MESSAGE" && \
	git commit -m "$$COMMIT_MESSAGE" && \
	git push

##@ Version Management
# Usage: make version V=0.6.0
# Validates PEP 440 compliance before updating (e.g., 0.5.3, 1.0.0, 0.6.0.dev1, 1.0.0rc1)
.PHONY: version
version: ## Update version across all packages (Usage: make version V=x.y.z)
ifndef V
	$(error VERSION is not set. Usage: make version V=x.y.z)
endif
	@$(PYTHON) -c "from packaging.version import Version; Version('$(V)')" 2>/dev/null || \
		(echo -e "$(RED)ERROR: '$(V)' is not a valid PEP 440 version.$(NC)" && \
		 echo -e "$(YELLOW)Valid examples: 0.5.3, 1.0.0, 0.6.0.dev1, 1.0.0a1, 1.0.0rc1, 1.0.0.post1$(NC)" && \
		 echo -e "$(YELLOW)Invalid examples: 0.5.3.wip5, 1.0-beta, v1.0.0$(NC)" && \
		 exit 1)
	@echo "Updating version to $(V)..."
	@echo "$(V)" > VERSION
	@sed -i.bak 's/^version = ".*"/version = "$(V)"/' lib/idp_cli_pkg/pyproject.toml && rm -f lib/idp_cli_pkg/pyproject.toml.bak
	@sed -i.bak 's/^version = ".*"/version = "$(V)"/' lib/idp_sdk/pyproject.toml && rm -f lib/idp_sdk/pyproject.toml.bak
	@sed -i.bak 's/^version = ".*"/version = "$(V)"/' lib/idp_common_pkg/pyproject.toml && rm -f lib/idp_common_pkg/pyproject.toml.bak
	@sed -i.bak 's/@click.version_option(version=".*")/@click.version_option(version="$(V)")/' lib/idp_cli_pkg/idp_cli/cli.py && rm -f lib/idp_cli_pkg/idp_cli/cli.py.bak
	@sed -i.bak 's/^__version__ = ".*"/__version__ = "$(V)"/' lib/idp_sdk/idp_sdk/__init__.py && rm -f lib/idp_sdk/idp_sdk/__init__.py.bak
	@sed -i.bak 's/^version = ".*"/version = "$(V)"/' lib/idp_mcp_connector_pkg/pyproject.toml && rm -f lib/idp_mcp_connector_pkg/pyproject.toml.bak
	@sed -i.bak 's/^__version__ = ".*"/__version__ = "$(V)"/' lib/idp_mcp_connector_pkg/idp_mcp_connector/__init__.py && rm -f lib/idp_mcp_connector_pkg/idp_mcp_connector/__init__.py.bak
	@sed -i.bak 's/^version = ".*"/version = "$(V)"/' lib/idp_feature_sdk/pyproject.toml && rm -f lib/idp_feature_sdk/pyproject.toml.bak
	@sed -i.bak 's/^__version__ = ".*"/__version__ = "$(V)"/' lib/idp_feature_sdk/idp_feature_sdk/__init__.py && rm -f lib/idp_feature_sdk/idp_feature_sdk/__init__.py.bak
	@# Seller Entitlement Service template. Deployed directly by a seller (sam
	@# deploy / idp-feature-cli), NOT via `idp-cli publish`, so it carries a
	@# literal version rather than the `<VERSION>` placeholder the published host
	@# template uses — nothing would substitute a placeholder here.
	@$(PYTHON) -c "import re,pathlib; p=pathlib.Path('feature-platform/seller-entitlement-service/template.yaml'); t=p.read_text(); n,c=re.subn(r\"(ServiceVersion:\\s*\\n\\s*Value: )'[^']*'\", r\"\\g<1>'$(V)'\", t); p.write_text(n); raise SystemExit(0 if c==1 else f'ERROR: expected 1 ServiceVersion replacement, made {c}')"
	@echo -e "$(GREEN)✅ Version updated to $(V) in:$(NC)"
	@echo "  - VERSION"
	@echo "  - lib/idp_cli_pkg/pyproject.toml"
	@echo "  - lib/idp_cli_pkg/idp_cli/cli.py"
	@echo "  - lib/idp_sdk/pyproject.toml"
	@echo "  - lib/idp_sdk/idp_sdk/__init__.py"
	@echo "  - lib/idp_common_pkg/pyproject.toml"
	@echo "  - lib/idp_mcp_connector_pkg/pyproject.toml"
	@echo "  - lib/idp_mcp_connector_pkg/idp_mcp_connector/__init__.py"
	@echo "  - lib/idp_feature_sdk/pyproject.toml"
	@echo "  - lib/idp_feature_sdk/idp_feature_sdk/__init__.py"
	@echo "  - feature-platform/seller-entitlement-service/template.yaml"


##@ Documentation
docs: docs-build ## Build and serve the documentation site locally
	@echo "Starting docs preview server..."
	cd docs-site && npm run preview

docs-setup: ## One-time docs site setup (symlinks + npm install)
	@echo "Setting up documentation site..."
	cd docs-site && bash setup.sh && npm install
	@echo -e "$(GREEN)✅ Docs site setup complete!$(NC)"

docs-build: docs-setup ## Build documentation site (no serve)
	@echo "Syncing sidebar with new docs..."
	cd docs-site && node sync-sidebar.mjs
	@echo "Building documentation site..."
	cd docs-site && npm run build
	@echo -e "$(GREEN)✅ Docs site built! $(NC)"
	@echo "Preview at: http://localhost:4321"

docs-deploy: docs-build ## Deploy docs to GitHub Pages (from local build)
	@echo "Deploying documentation site to GitHub Pages..."
	touch docs-site/dist/.nojekyll
	cd docs-site && npx gh-pages -d dist --dotfiles --repo https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws.git
	@echo -e "$(GREEN)✅ Docs deployed to GitHub Pages!$(NC)"

##@ Security (SRT)
srt-clean: ## Remove gitignored build/temp dirs that pollute local SRT scans
	@echo "Removing build artifacts that pollute SRT scans..."
	find . -name node_modules -prune -o -name .venv -prune -o \
		-type d \( -name .aws-sam -o -path '*/layer/python' \) -prune -print \
		| xargs -r rm -rf
	@echo -e "$(GREEN)✅ Scan-polluting artifacts removed (CI checkouts are already clean)$(NC)"

srt: ## Run full SRT workflow (clean → setup → scan → optional fix)
	@$(MAKE) srt-clean
	@$(MAKE) srt-setup
	@$(MAKE) srt-scan
	@echo ""
	@echo "Do you want to run SRT fix? (y/N):"
	@read answer && \
	if [ "$$answer" = "y" ] || [ "$$answer" = "Y" ]; then \
		$(MAKE) srt-fix; \
	fi

srt-setup: ## Download and configure SRT tool
	@echo "Setting up SRT tool..."
	$(PYTHON) scripts/srt/setup.py

srt-scan: ## Run SRT security assessment
	@echo "Running SRT security assessment..."
	$(PYTHON) scripts/srt/run.py

srt-fix: ## Run SRT interactive fix
	@echo "Running SRT interactive fix..."
	$(PYTHON) scripts/srt/fix.py

security-results: ## Run security tests + curate a public-safe snapshot into security/test-results/<version>/ (STACK_NAME=... for the live ZAP+RBAC tests; omit for offline-only)
	PYTHON="$(PYTHON)" bash scripts/security/run_security_tests.sh

##@ Dependencies
dep-manifest: ## Generate dependency manifests for artifact repository mirroring (Python + Node)
	@bash scripts/generate-dep-manifest.sh

# SRT (the security gate) runs syft, which inventories dependencies but does no
# vulnerability matching — so nothing in CI used to fail on a known-vulnerable
# pin. This target closes that gap by matching the generated manifests against
# OSV. Findings that are genuinely unreachable go in
# scripts/security/dep_audit_allowlist.json WITH a justification.
dep-audit: ## Audit all pinned Python + Node dependencies against OSV (fails on HIGH+)
	@$(PYTHON) scripts/security/dep_audit.py

dep-audit-fast: ## Same as dep-audit but reuses existing dist/manifests (no regeneration)
	@$(PYTHON) scripts/security/dep_audit.py --no-generate

##@ Deploy
# Thin wrappers around `idp-cli publish` / `deploy` / `delete` for the common
# 80% case. Uncommon flags can still be passed via EXTRA_ARGS="--foo --bar".
# See 'docs/idp-cli.md' (or 'idp-cli <cmd> --help') for the full option list.

.PHONY: publish deploy delete-stack

# Usage examples:
#   make publish REGION=us-east-1
#   make publish REGION=us-east-1 BUCKET_BASENAME=my-idp-artifacts PREFIX=v1
#   make publish REGION=us-gov-west-1 HEADLESS=1
#   make publish REGION=us-east-1 PUBLIC=1 EXTRA_ARGS="--clean-build --verbose"
publish: ## Build & publish IDP artifacts to S3 (Usage: make publish REGION=... [BUCKET_BASENAME=...] [PREFIX=...] [HEADLESS=1] [PUBLIC=1] [EXTRA_ARGS=...])
ifndef REGION
	$(error REGION is not set. Usage: make publish REGION=us-east-1 [BUCKET_BASENAME=...] [PREFIX=...] [HEADLESS=1] [PUBLIC=1] [EXTRA_ARGS=...])
endif
	@echo -e "$(CYAN)Running idp-cli publish (region=$(REGION))...$(NC)"
	$(IDP_CLI) publish \
		--source-dir . \
		--region $(REGION) \
		$(if $(BUCKET_BASENAME),--bucket-basename $(BUCKET_BASENAME)) \
		$(if $(PREFIX),--prefix $(PREFIX)) \
		$(if $(HEADLESS),--headless) \
		$(if $(PUBLIC),--public) \
		$(EXTRA_ARGS)

# Usage examples:
#   make deploy STACK_NAME=my-idp ADMIN_EMAIL=me@example.com                 # create new stack
#   make deploy STACK_NAME=my-idp                                             # update existing stack
#   make deploy STACK_NAME=my-idp-dev ADMIN_EMAIL=me@example.com FROM_CODE=1  # build & deploy from local source
#   make deploy STACK_NAME=my-idp ADMIN_EMAIL=me@example.com HEADLESS=1       # headless (no UI)
#   make deploy STACK_NAME=my-idp CUSTOM_CONFIG=./my-config.yaml              # update config on existing stack
#   make deploy STACK_NAME=my-idp TAGS="Owner=docs-team,Environment=prod"     # stack tags (propagated to all resources)
#   make deploy STACK_NAME=my-idp NO_WAIT=1                                   # fire-and-forget (default is --wait)
#   make deploy STACK_NAME=my-idp EXTRA_ARGS="--max-concurrent 200 --log-level DEBUG"
deploy: ## Deploy/update IDP CloudFormation stack (Usage: make deploy STACK_NAME=... [ADMIN_EMAIL=...] [REGION=...] [FROM_CODE=1] [HEADLESS=1] [CUSTOM_CONFIG=...] [TAGS=...] [TEMPLATE_URL=...] [TEMPLATE_FILE=...] [NO_WAIT=1] [EXTRA_ARGS=...])
ifndef STACK_NAME
	$(error STACK_NAME is not set. Usage: make deploy STACK_NAME=my-stack [ADMIN_EMAIL=...] [REGION=...] [FROM_CODE=1] [HEADLESS=1] [CUSTOM_CONFIG=...] [TAGS=...] [NO_WAIT=1] [EXTRA_ARGS=...])
endif
	@echo -e "$(CYAN)Running idp-cli deploy (stack=$(STACK_NAME))...$(NC)"
	$(IDP_CLI) deploy \
		--stack-name $(STACK_NAME) \
		$(if $(ADMIN_EMAIL),--admin-email $(ADMIN_EMAIL)) \
		$(if $(REGION),--region $(REGION)) \
		$(if $(FROM_CODE),--from-code .) \
		$(if $(HEADLESS),--headless) \
		$(if $(CUSTOM_CONFIG),--custom-config $(CUSTOM_CONFIG)) \
		$(if $(TAGS),--tags "$(TAGS)") \
		$(if $(TEMPLATE_URL),--template-url $(TEMPLATE_URL)) \
		$(if $(TEMPLATE_FILE),--template-file $(TEMPLATE_FILE)) \
		$(if $(NO_WAIT),,--wait) \
		$(EXTRA_ARGS)

# Usage examples:
#   make delete-stack STACK_NAME=test-stack                                   # interactive
#   make delete-stack STACK_NAME=test-stack FORCE=1                            # skip confirmation
#   make delete-stack STACK_NAME=test-stack FORCE=1 EMPTY_BUCKETS=1            # empty buckets first
#   make delete-stack STACK_NAME=test-stack FORCE=1 FORCE_DELETE_ALL=1         # comprehensive cleanup
delete-stack: ## Delete an IDP CloudFormation stack (Usage: make delete-stack STACK_NAME=... [FORCE=1] [EMPTY_BUCKETS=1] [FORCE_DELETE_ALL=1] [REGION=...] [NO_WAIT=1] [EXTRA_ARGS=...])
ifndef STACK_NAME
	$(error STACK_NAME is not set. Usage: make delete-stack STACK_NAME=my-stack [FORCE=1] [EMPTY_BUCKETS=1] [FORCE_DELETE_ALL=1])
endif
	@echo -e "$(YELLOW)Running idp-cli delete (stack=$(STACK_NAME))...$(NC)"
	$(IDP_CLI) delete \
		--stack-name $(STACK_NAME) \
		$(if $(FORCE),--force) \
		$(if $(EMPTY_BUCKETS),--empty-buckets) \
		$(if $(FORCE_DELETE_ALL),--force-delete-all) \
		$(if $(REGION),--region $(REGION)) \
		$(if $(NO_WAIT),,--wait) \
		$(EXTRA_ARGS)



##@ Marketplace (seller-side)
# The Seller Entitlement Service is deployed by an extension SELLER into their
# OWN AWS Marketplace seller account — not into a customer account, and not as
# part of the IDP main stack. See
# feature-platform/seller-entitlement-service/README.md.
#
# These are thin wrappers around `idp-feature-cli seller-service`, which is where
# the logic lives: the audience is extension authors (who already use that CLI to
# publish and deploy), and the preflight is a safety guard that deserves unit
# tests — which a shell snippet in a Makefile would not get.

.PHONY: seller-entitlement-service seller-entitlement-service-preflight seller-entitlement-service-activations

# Usage:
#   make seller-entitlement-service-preflight PRODUCT_REGISTRY='{"prod-xxx":{"productCode":"yyy"}}'
seller-entitlement-service-preflight: ## Check current creds are the SELLER for these products (read-only) (Usage: make seller-entitlement-service-preflight PRODUCT_REGISTRY='{...}' [SELLER_ACCOUNT_ID=...] [REGION=...])
ifndef PRODUCT_REGISTRY
	$(error PRODUCT_REGISTRY is not set. Usage: make seller-entitlement-service-preflight PRODUCT_REGISTRY='{"prod-xxx":{"productCode":"yyy","allowFreeTier":true}}')
endif
	@$(IDP_FEATURE_CLI) seller-service preflight \
		--product-registry '$(PRODUCT_REGISTRY)' \
		$(if $(SELLER_ACCOUNT_ID),--seller-account-id $(SELLER_ACCOUNT_ID)) \
		$(if $(REGION),--region $(REGION)) \
		$(if $(SKIP_OWNERSHIP_CHECK),--skip-ownership-check) \
		$(EXTRA_ARGS)

# Usage:
#   make seller-entitlement-service PRODUCT_REGISTRY='{"prod-xxx":{"productCode":"yyy","allowFreeTier":true}}'
#   make seller-entitlement-service PRODUCT_REGISTRY='{...}' SELLER_ACCOUNT_ID=145026617366 YES=1
seller-entitlement-service: ## Preflight + deploy the Seller Entitlement Service into the SELLER account (Usage: make seller-entitlement-service PRODUCT_REGISTRY='{...}' [STACK_NAME=...] [SELLER_ACCOUNT_ID=...] [REGION=...] [YES=1])
ifndef PRODUCT_REGISTRY
	$(error PRODUCT_REGISTRY is not set. Usage: make seller-entitlement-service PRODUCT_REGISTRY='{"prod-xxx":{"productCode":"yyy","allowFreeTier":true}}')
endif
	@$(IDP_FEATURE_CLI) seller-service deploy \
		--product-registry '$(PRODUCT_REGISTRY)' \
		$(if $(STACK_NAME),--stack-name $(STACK_NAME)) \
		$(if $(SELLER_ACCOUNT_ID),--seller-account-id $(SELLER_ACCOUNT_ID)) \
		$(if $(REGION),--region $(REGION)) \
		$(if $(ALLOWED_ACCOUNTS),--allowed-accounts $(ALLOWED_ACCOUNTS)) \
		$(if $(TOKEN_TTL_SECONDS),--token-ttl-seconds $(TOKEN_TTL_SECONDS)) \
		$(if $(SKIP_OWNERSHIP_CHECK),--skip-ownership-check) \
		$(if $(YES),--yes) \
		$(EXTRA_ARGS)
# Usage:
#   make seller-entitlement-service-activations
#   make seller-entitlement-service-activations PRODUCT_ID=prod-xxx OUTCOME=refused
seller-entitlement-service-activations: ## Show which buyer accounts activated / were refused which products (Usage: make seller-entitlement-service-activations [PRODUCT_ID=...] [OUTCOME=granted|refused] [SINCE=...] [REGION=...])
	@$(IDP_FEATURE_CLI) seller-service activations \
		$(if $(STACK_NAME),--stack-name $(STACK_NAME)) \
		$(if $(PRODUCT_ID),--product-id $(PRODUCT_ID)) \
		$(if $(BUYER_ACCOUNT_ID),--buyer-account-id $(BUYER_ACCOUNT_ID)) \
		$(if $(OUTCOME),--outcome $(OUTCOME)) \
		$(if $(SINCE),--since $(SINCE)) \
		$(if $(REGION),--region $(REGION)) \
		$(EXTRA_ARGS)


##@ Benchmarking

.PHONY: benchmark-release
# The release-cycle benchmark is skill-driven (it needs judgment: cross-version config
# compatibility, corefast scoping, failure honesty). This target is a thin wrapper that
# invokes Claude Code to run the `run-benchmarks` skill for a prev-published-vs-develop
# comparison, producing docs/benchmarking/releases/v<VERSION>.md.
#
# Usage:
#   make benchmark-release VERSION=0.6.0 PREV=0.5.16
#   make benchmark-release VERSION=0.6.0 PREV=0.5.16 STACK_NAME=idpbench0516   # reuse a stack
benchmark-release: ## Run the release-vs-release benchmark audit trail (Usage: make benchmark-release VERSION=... PREV=... [STACK_NAME=...])
ifndef VERSION
	$(error VERSION is not set. Usage: make benchmark-release VERSION=0.6.0 PREV=0.5.16)
endif
ifndef PREV
	$(error PREV is not set (previous PUBLISHED release). Usage: make benchmark-release VERSION=0.6.0 PREV=0.5.16)
endif
	@command -v claude >/dev/null 2>&1 || { echo -e "$(RED)claude CLI not found. Run the 'run-benchmarks' skill manually (see .claude/skills/run-benchmarks.md).$(NC)"; exit 1; }
	@echo -e "$(CYAN)Invoking Claude Code to run the release benchmark (v$(PREV) published -> v$(VERSION) develop)...$(NC)"
	@echo -e "$(YELLOW)This deploys/upgrades a real stack and runs live Bedrock jobs (~1-2h, costs \$$). Ctrl-C to abort.$(NC)"
	claude --dangerously-skip-permissions -p "Use the run-benchmarks skill to produce the release-cycle audit-trail entry comparing the previous PUBLISHED release v$(PREV) to the current develop prerelease v$(VERSION). Follow the skill's 'Release-cycle audit trail' procedure end to end: deploy the published v$(PREV) template$(if $(STACK_NAME), (reuse stack $(STACK_NAME))), run the corefast suite with --native-upload, save + promote baseline, upgrade the SAME stack in place to develop via --from-code --clean-build, re-run corefast, aggregate + compare + figures, then write docs/benchmarking/releases/v$(VERSION).md and append a row to docs/benchmarking/releases/README.md. Work autonomously and report the deltas."
