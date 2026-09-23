# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# One definition of "the AWS environment a CI runner has", shared by every
# offline test target in this repository.
#
# Neutralise every source botocore consults for a region, credentials or a
# profile, so the offline suites run the way a CI runner runs them.
#
# The suites under `test-packages-cicd` and `lib/idp_common_pkg`'s unit targets
# are offline by contract: no AWS call, no credentials. Nothing enforced that, and
# the two ways it broke are both invisible on the machine where the code is
# written:
#
#  * A suite needed a REGION, because a handler it imports builds a boto3 client
#    at module scope. It passed on a developer machine, which supplies one from
#    the shared AWS config file, and failed on a runner, which supplies nothing.
#    Three suites were in that state (#988); each now pins a region in its own
#    conftest.py.
#  * A suite needed CREDENTIALS, and got them from the EC2 instance metadata
#    service — a source a developer box on EC2 has and a runner does not. botocore
#    freezes the session's credentials object into a client when the client is
#    constructed, so a client built at import time with none resolvable can never
#    sign, however many credentials appear later. Five tests in
#    `lib/idp_common_pkg` failed exactly that way, on CI only.
#
# This wrapper is what keeps the next one from reaching CI undetected, by making
# the local run equal the CI run instead of being weaker than it.
#
# Two kinds of neutralisation are needed, because botocore has more than one
# source. The `-u` list removes the environment variables. Pointing
# AWS_CONFIG_FILE and AWS_SHARED_CREDENTIALS_FILE at an empty file removes the
# shared config file, which no amount of unsetting can reach. Disabling the
# instance metadata service removes the third, which is also what stops a
# credential lookup from stalling when these run on EC2.
#
# Removing the credentials as well as the region is deliberate: a suite here that
# reaches a real AWS endpoint should fail loudly rather than quietly transact
# against whichever account the developer happens to be signed in to. Suites that
# legitimately need placeholder credentials set them themselves, in their own
# conftest.py, where `os.environ.setdefault` reinstates them after this wrapper
# has taken the machine's real ones away.
#
# scripts/tests/test_offline_suites_are_hermetic.py parses these two definitions
# out of this file and asserts both that the wrapper still works and that every
# pytest invocation in the gated recipes still goes through it.
HERMETIC_AWS := env -u AWS_REGION -u AWS_DEFAULT_REGION -u AWS_PROFILE \
	-u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
	-u AWS_SECURITY_TOKEN -u AWS_ROLE_ARN -u AWS_WEB_IDENTITY_TOKEN_FILE \
	-u AWS_CONTAINER_CREDENTIALS_FULL_URI \
	-u AWS_CONTAINER_CREDENTIALS_RELATIVE_URI \
	AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null \
	AWS_EC2_METADATA_DISABLED=true

# The checkout this file belongs to, resolved from this file's own path rather
# than from the working directory, because the two including Makefiles are
# entered from different directories (`make` at the root, `make -C
# lib/idp_common_pkg` from CI).
FIRST_PARTY_CHECKOUT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))..)

# Every first-party package root in THIS checkout, pinned onto PYTHONPATH for
# every pytest invocation below.
#
# `import idp_common` does not read the checkout a suite lives in. These packages
# are editable installs, so the import follows whichever pointer currently sits in
# the active interpreter's site-packages — and where `python3` resolves to an
# interpreter shared between checkouts, every `pip install -e` on the host
# rewrites that pointer for all of them, last writer wins. One of this
# repository's own gates is a writer: `lib/idp_common_pkg`'s `test-unit-cicd`
# reinstalls unless SKIP_INSTALL=1, so running the test gate repoints it at
# whichever checkout ran it. A suite that then runs unpinned elsewhere measures
# that tree. It does not announce itself either: the imported package is a real,
# self-consistent revision of this one, so most tests still pass and the run reads
# green while describing other code (#1094).
#
# Three properties this pin has to have, each of which was a way of getting it
# wrong:
#
#  * ABSOLUTE. A relative entry does not survive into a subprocess that runs with
#    a different working directory, which several suites here start.
#  * ALL of them, not just idp_common. The packages import each other, so pinning
#    one leaves the rest resolving wherever they were pointing, and the provenance
#    guard in scripts/tests/conftest.py then refuses the run.
#  * DERIVED, not listed. `lib/*/pyproject.toml` is what makes a directory an
#    installable first-party root, so the same rule that decides what
#    FIRST_PARTY_EDITABLES installs decides what is pinned here, and a package
#    added to lib/ is covered without anyone remembering to add it.
#    scripts/tests/test_first_party_pythonpath.py asserts this expansion, the
#    equivalent rule in Python, and FIRST_PARTY_EDITABLES all name the same set.
_FIRST_PARTY_EMPTY :=
_FIRST_PARTY_SPACE := $(_FIRST_PARTY_EMPTY) $(_FIRST_PARTY_EMPTY)
FIRST_PARTY_ROOTS := $(patsubst %/,%,$(dir \
	$(wildcard $(FIRST_PARTY_CHECKOUT)/lib/*/pyproject.toml)))
FIRST_PARTY_PYTHONPATH ?= $(subst $(_FIRST_PARTY_SPACE),:,$(strip $(FIRST_PARTY_ROOTS)))

# Recursive (`=`, not `:=`) so $(PYTHON) resolves in whichever Makefile includes
# this, at the point of use — the two including Makefiles derive it differently.
#
# The caller's own PYTHONPATH is appended rather than dropped, so a pin a
# developer set for some other reason still applies — after this one, which is the
# order that makes the checkout under test win. `FIRST_PARTY_PYTHONPATH=` (empty)
# suppresses the pin entirely, for the deliberate case of testing an installed
# copy; the `$(if ...)` is what keeps that from exporting an empty PYTHONPATH,
# whose first entry is the working directory.
PYTEST_HERMETIC = $(HERMETIC_AWS) $(if $(FIRST_PARTY_PYTHONPATH),\
	PYTHONPATH=$(FIRST_PARTY_PYTHONPATH)$${PYTHONPATH:+:$$PYTHONPATH}) \
	$(PYTHON) -m pytest
