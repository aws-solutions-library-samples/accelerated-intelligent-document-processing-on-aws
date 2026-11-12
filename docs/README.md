---
title: "GenAIIDP Documentation"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# GenAIIDP Documentation

This folder contains detailed documentation on various aspects of the GenAI Intelligent Document Processing solution.

## Core

- [Architecture](./architecture.md) - Detailed component architecture and data flow
- [Quick Start](./quick-start.md) - Cold-start configuration authoring for new deployments (chat widget + `idp-cli bootstrap`)
- [Deployment](./deployment.md) - Build, publish, deploy, and test instructions
- [AI-Guided Deployment Walkthrough](./idp-deployment-ai-guide.md) - A system prompt to paste into an AI assistant that walks you through choosing deployment parameters one question at a time
- [After Subscribing on AWS Marketplace](./marketplace-subscription-next-steps.md) - What to do next in your own AWS account after a Marketplace subscription
- [Terraform Deployment](./terraform.md) - Experimental: deploy the generated Terraform module with Terraform or OpenTofu, or with `idp-cli deploy --terraform`
- [Migrating from genai-idp-terraform](./terraform-migration.md) - Move a deployment of the separate `awslabs/genai-idp-terraform` repository onto the built-in module in place, keeping its data and users
- [Headless Deployment](./headless-deployment.md) - Backend-only deployment (no Web UI, no UI REST API, no Cognito, no WAF) — for API-only / pipeline integrations in Commercial regions or GovCloud
- [Configuration](./configuration.md) - Configuration and customization options
- [Configuration Profiles](./configuration-profiles.md) - Managing multiple named configurations and their revision history
- [Multi-Tenant Isolation](./multi-tenancy.md) - Tenant declarations, strict isolation mode, and hardening a shared deployment in your own AWS environment
- [IDP Configuration Best Practices](./idp-configuration-best-practices.md) - Guidelines for effective configuration design
- [JSON Schema Migration](./json-schema-migration.md) - JSON Schema format guide and legacy migration details
- [Web UI](./web-ui.md) - Web interface features and usage
- [IDP CLI](./idp-cli.md) - Command line interface for batch processing and evaluation workflows
- [IDP SDK](./idp-sdk.md) - Python SDK for programmatic access
- [idp_common API Reference](./idpcommon-api-reference.md) - The core shared Python library: its services per pipeline step, the `Document` model and utilities
- [Demo Videos](./demo-videos.md) - Comprehensive collection of feature demonstration videos
- [Hands-on Workshops](./workshops.md) - The GenAI IDP Workshop on AWS Workshop Studio and the self-guided Amazon Quick integration workshop
- [Troubleshooting](./troubleshooting.md) - Troubleshooting and performance guides
- [Error Analyzer](./error-analyzer.md) - AI-powered error diagnosis

## Processing Modes

- [BDA Mode Reference](./pattern-1.md) - Bedrock Data Automation (BDA) concepts and behavior
- [Pipeline Mode Reference](./pattern-2.md) - Textract + Bedrock classification and extraction
- [Discovery](./discovery.md) - Pattern-neutral discovery process and BDA blueprint automation

## Document Processing Features

- [Classification](./classification.md) - Customizing document classification
- [Extraction & Confidence](./extraction-and-confidence.md) - Extraction configuration, per-field confidence scoring, and bounding-box geometry (consolidates the former Extraction, Assessment, and Assessment Bounding Boxes guides)
- [Schema Enforcement](./schema-enforcement.md) - Make the extraction reply match the document schema: Bedrock structured outputs, a forced tool call, or auto
- [Extraction Scaling Guide](./extraction-scaling-guide.md) - Simple vs advanced (agentic) mode limits by document/list size; where each hits limits and how to choose
- [Missing Pages vs. Blank Fields](./missing-page-handling.md) - Telling a legitimately omitted page apart from an empty field on sparsely populated forms
- [Few-Shot Examples](./few-shot-examples.md) - Implementing few-shot examples for improved accuracy
- [Human-in-the-Loop Review](./human-review.md) - Human review workflows with built-in review system
- [Review Queues](./review-queues.md) - System-wide HITL review queues; Admins assign items to Annotators; the review status contract
- [Document Versions](./document-versions.md) - Each processing run of a document is kept as a version, so reprocessing or re-uploading does not lose prior results
- [Rule Validation](./rule-validation.md) - Business rule validation and compliance checking
- [Z3 Symbolic Rule Validation Engine](./rule-validation-z3.md) - Deterministic, formal rule validation with the Z3 theorem prover, routed per rule alongside the LLM engine
- [Policy Discovery](./policy-discovery.md) - Extract business rules from policy documents into a configuration profile for rule validation
- [OCR Image Sizing Guide](./ocr-image-sizing-guide.md) - Optimizing image sizes for OCR processing
- [Languages](./languages.md) - Multi-language support

## Evaluation & Testing

- [Testing](./testing.md) - Every test layer and tier in this repo, what it proves, and whether CI runs it
- [Evaluation Framework](./evaluation.md) - Accuracy assessment system powered by Stickler
- [Evaluation Enhanced Reporting](./evaluation-enhanced-reporting.md) - Advanced evaluation reports with field-level comparisons
- [Test Studio](./test-studio.md) - Interactive testing with curated datasets
- [Creating Custom Test Sets](./creating-custom-test-sets.md) - End-to-end workflow for building a test set with ground truth from scratch
- [MLflow Experiment Tracking](./mlflow-integration.md) - Optional logging of test-run metrics and configuration to Amazon SageMaker with MLflow
- [Benchmarking Guide](./benchmarking/index.md) - The repeatable benchmark harness across document sizes and configuration options, and how to read its results
- [Configuration Guidance](./benchmarking/config-guidance.md) - Measured guidance for choosing configuration options, from the benchmark suite
- [Benchmark Studies](./benchmarking/studies/README.md) - Single-question measurements made during development (confidence calibration, prompt caching and others)
- [Release Benchmark Audit Trail](./benchmarking/releases/README.md) - One benchmark comparison per release against the previous published release
- [Release Validation Records](./release-validation/README.md) - One record per release of the tests that need a real deployed stack

## AI Agents & Analytics

- [Agent Analysis](./agent-analysis.md) - Natural language analytics and data visualization feature
- [Agent Companion Chat](./agent-companion-chat.md) - Conversational agent for document analysis
- [Code Intelligence](./code-intelligence.md) - Chat bot for asking questions about the IDP code base and features
- [Knowledge Base](./knowledge-base.md) - Document knowledge base query feature
- [Custom MCP Agent](./custom-MCP-agent.md) - Integrating external MCP servers for custom tools and capabilities
- [MCP Server](./mcp-server.md) - Model Context Protocol integration for external applications
- [IDP MCP Connector](./mcp-connector.md) - Local tool that connects IDE-based AI coding assistants to the solution's MCP server

## Integration & Extensions

- [Post-Processing Lambda Hook](./post-processing-lambda-hook.md) - Custom downstream processing integration
- [Lambda Hook Inference](./lambda-hook-inference.md) - Custom LLM integration via Lambda hooks
- [Nova Fine-Tuning](./nova-finetuning.md) - Fine-tuning Amazon Nova models for IDP tasks
- [Custom Model Fine-Tuning](./custom-model-finetuning.md) - Fine-tuning Nova models for document classification with the accelerator
- [OpenAI Models](./openai-models.md) - GPT-6 Astra via the Converse API (CRIS-only `us.`/`global.` IDs, agentic extraction, all regions) **and** GPT-5.4 / GPT-5.5 / GPT-5.6 (Sol/Terra/Luna) via the bedrock-mantle Responses API: the two-path split, support matrices, limitations, regions, caching, long-context pricing
- [xAI Grok Models](./grok-models.md) - Grok 4.6 via the Converse API (CRIS-only `us.`/`global.` IDs): support matrix, agentic extraction, reasoning effort, tier and caching caveats
- [Service Tiers](./service-tiers.md) - Configurable service tier options
- [Feature Platform](./feature-platform.md) - The framework for installable extensions (preview): catalog, installation and runtime behavior
- [Feature Platform Developer Guide](./feature-platform-developer-guide.md) - How to build an extension and add it to the catalog

### Extensions

- [Auto Optimizer (Beta)](./extensions/auto-optimizer.md) - Agentic optimization of a document processing configuration
- [Test Set - ConfBench](./extensions/confbench-testset.md) - Deploys the ConfBench benchmark into Test Studio on demand
- [Test Set Generator](./extensions/idp-data-generator.md) - Generates labeled synthetic document test sets
- [IDP Monitor](./extensions/idp-monitor.md) - AI-assisted operations dashboard over the reporting data
- [PII Anonymization](./extensions/pii-anonymizer.md) - Experimental PII redaction as a preprocessing hook
- [Sample: Document Status](./extensions/sample-document-status.md) - The minimal feature add-on sample
- [Sample: Health Insurance Review](./extensions/sample-health-insurance-review.md) - The use-case extension sample

## Monitoring & Operations

- [Monitoring](./monitoring.md) - Monitoring and logging capabilities
- [Reporting Database](./reporting-database.md) - Analytics database for evaluation metrics and metering data
- [Reporting SQL Layer](./reporting-sql-layer.md) - Athena rollup tables over the raw metering data for cost and volume questions across wide time ranges
- [Data-Mart Migration Runbook](./data-mart-migration-runbook.md) - Operating the state machine that repopulates the per-document rollup tables
- [Capacity Planning](./capacity-planning.md) - Performance optimization and resource scaling guidance
- [Circuit Breaker](./circuit-breaker.md) - Automatic protection from cascading failures during Bedrock outages
- [Cross-Account Bedrock](./cross-account-bedrock.md) - Route all Bedrock invocations through a centralized hub account via STS AssumeRole
- [Cost Calculator](./cost-calculator.md) - Framework for estimating solution costs
- [Release Runbook](./release-runbook.md) - How a release is built and published to S3, for publishing releases to buckets in your own account: preconditions, ordered steps, failure and recovery, post-release verification
- [Version Update Indicator](./version-update-indicator.md) - The Web UI badge shown when a newer template version has been published

## Planning & Security

- [Threat Model](./threat-model.md) - STRIDE threat model: trust boundaries, controls in place, residual risks and open items, and the currency gate that keeps it from going stale
- [Well-Architected Framework Review](./well-architected.md) - What the solution implements per pillar, plus a checklist template you fill in for your own deployment
- [AWS Services & IAM Roles](./aws-services-and-roles.md) - AWS services used and IAM role requirements
- [Role-Based Access Control](./rbac.md) - Server-side role enforcement at the API layer, UI controls, and configuration-profile scoping
- [External Identity Provider](./external-idp.md) - Federating sign-in from a SAML or OIDC identity provider through Amazon Cognito
- [Malware Scanning](./malware-scanning.md) - Scanning incoming documents before they are processed
- [VPC Secured Mode](./vpc-secured-mode.md) - Running the solution in a VPC-secured environment for network isolation
- [Deploying in a Private Network](./deployment-private-network.md) - Runbook for a fully private or air-gapped deployment
- [Installing First-Party Packages Safely](./dependency-confusion.md) - Why the packages in `lib/` must be installed from a local checkout, never by bare name
- [Dependency Mirroring](./dependency-mirroring.md) - Listing every Python and Node.js dependency for mirroring into an artifact repository
- [API Gateway Hosting](./apigateway-hosting.md) - Serve the full Web UI from the existing API Gateway REST API (S3 proxy), within a VPC when combined with `ApiGatewayVisibility=PRIVATE` (alternative to CloudFront)
- [GovCloud Deployment](./govcloud-deployment.md) - Deploy to GovCloud with the full Web UI (`--govcloud`) or headless (`--headless`)
- [EU Region Model Support](./eu-region-model-support.md) - Model availability in EU regions
- [Canada (Central) Region Model Support](./canada-region-model-support.md) - Model availability and default model changes in ca-central-1

## Development Setup

- [Setup: Linux](./setup-development-env-linux.md) - Development environment setup for Linux
- [Setup: macOS](./setup-development-env-macos.md) - Development environment setup for macOS
- [Setup: WSL](./setup-development-env-WSL.md) - Development environment setup for Windows Subsystem for Linux
- [Setup: Windows](./setup-development-env-windows.md) - Native Windows development environment setup
- [Using Notebooks](./using-notebooks-with-idp-common.md) - Guide for using Jupyter notebooks with the IDP Common Library

## Migration

- [Migration v0.6.12 to v0.6.13](./migration-v0612-to-v0613.md) - Classification settings nested by model call (config format 0.9)
- [Migration v0.5 to v0.6](./migration-v05-to-v06.md) - The standalone `assessment:` block folded into `extraction.confidence`, `extraction.geometry` and `hitl`
- [Granular Assessment Retirement](./migration-granular-retirement.md) - What the retirement of granular assessment means for your configuration
- [AppSync to REST API Migration](./migration-appsync-to-rest.md) - How the REST transport reproduces what the UI previously got from AWS AppSync
- [Migration v0.4 to v0.5](./migration-v04-to-v05.md) - Upgrading from v0.4.x to v0.5.x (Unified Pattern)

### GovCloud

- [GovCloud Deployment Guide](./govcloud-deployment.md) - Deployment options (Web UI or headless), prerequisites, and deploy commands
- [GovCloud Architecture](./govcloud-architecture.md) - Services removed vs. retained, limitations, and workarounds
- [Batch Jobs REST API](./govcloud-batch-api.md) - API reference, authentication, and bastion tunnel setup
- [GovCloud Operations](./govcloud-operations.md) - Monitoring, troubleshooting, and best practices

## Blogs, Customer Stories & Research

- [Blogs, Customer Stories & Research](./references.md) - AWS blog deep-dives into the accelerator, independent analyst reports, customer references with real-world metrics, and peer-reviewed research papers

## Screenshots and Diagrams

The documentation references several screenshots and diagrams from the `../images` folder:

- Unified architecture diagram (`IDP.UnifiedPatterns.drawio.png`)
- Web UI screenshots (`WebUI.png`)
- Dashboard screenshots (`Dashboard1.png`, `Dashboard2.png`, `Dashboard3.png`)

## Contributing to Documentation

When updating these documents:

1. Keep content concise and focused on the specific topic
2. Include relevant screenshots and diagrams where helpful
3. Use markdown formatting for readability
4. Cross-reference other documents where appropriate
5. Ensure new documents are listed in this README
