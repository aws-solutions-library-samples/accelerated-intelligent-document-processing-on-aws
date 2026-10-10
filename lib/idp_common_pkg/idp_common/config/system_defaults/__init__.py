# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
System defaults for IDP configuration.

This package contains YAML files with default configuration values
for all IDP patterns (pattern-1, pattern-2).

The defaults are organized into modular files:
- base.yaml: Composite that includes all modules
- base-notes.yaml: Configuration notes/description
- base-classes.yaml: Document class definitions
- base-ocr.yaml: Textract OCR configuration
- base-classification.yaml: LLM classification settings
- base-extraction.yaml: LLM extraction settings
- base-confidence.yaml: Per-field confidence assessment + HITL thresholds
- base-geometry.yaml: Field bounding-box geometry mode
- base-summarization.yaml: Document summarization
- base-chat.yaml: Chat-with-Document (interactive Q&A)
- base-evaluation.yaml: Evaluation/testing
- base-rule-validation.yaml: Rule validation settings
- base-agents.yaml: Error analyzer and chat companion
- base-discovery.yaml: Schema discovery
- base-rule-discovery.yaml: Rule discovery from policy documents
- pattern-1.yaml: BDA pattern (selective inheritance)
- pattern-2.yaml: Bedrock LLM pattern (full inheritance)

Usage:
    from idp_common.config.merge_utils import load_system_defaults

    defaults = load_system_defaults("pattern-2")
"""
