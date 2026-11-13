# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Custom SAM build (Metadata.BuildMethod: makefile). Installs the thin web stack
# (fastapi/uvicorn) and copies this package's sources into the SAM artifacts dir.
# idp_common comes from the attached Lambda layers.
#
# The two chat processor modules live under vendored/ (committed copies of
# src/lambda/{chat_with_document_processor,agent_chat_processor}/index.py, kept
# in sync by scripts/sync_chat_stream_vendored.sh and asserted by a unit test).
# They MUST live inside this package's CodeUri because SAM's makefile builder
# runs against an isolated copy of CodeUri only — sibling Lambda directories are
# not reachable at build time (that was the original build failure).

build-ChatStreamProcessorFunction:
	mkdir -p $(ARTIFACTS_DIR)
	cp $(CURDIR)/app.py $(ARTIFACTS_DIR)/
	cp $(CURDIR)/sse.py $(ARTIFACTS_DIR)/
	cp $(CURDIR)/run.sh $(ARTIFACTS_DIR)/
	chmod +x $(ARTIFACTS_DIR)/run.sh
	# Vendored processor sources, exposed under their importable module names.
	cp $(CURDIR)/vendored/chat_with_document_processor.py $(ARTIFACTS_DIR)/chat_with_document_processor.py
	cp $(CURDIR)/vendored/agent_chat_processor.py $(ARTIFACTS_DIR)/agent_chat_processor.py
	# Install for the Lambda x86_64 Linux runtime, NOT the build host. fastapi
	# pulls in pydantic/pydantic-core, which have compiled extensions; a plain
	# `pip install -t` on an ARM/Mac build host ships a darwin .so that cannot
	# load on Lambda (ModuleNotFoundError: pydantic_core._pydantic_core), and it
	# also shadows the correct Linux build in the attached layers. Cross-target
	# manylinux wheels only (matches the layer build in idp_sdk publish).
	python -m pip install -r $(CURDIR)/requirements.txt -t $(ARTIFACTS_DIR) \
		--platform manylinux2014_x86_64 \
		--python-version 3.12 \
		--implementation cp \
		--only-binary=:all: \
		--upgrade
