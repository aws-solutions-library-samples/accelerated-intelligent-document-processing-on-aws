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
- [Headless Deployment](./headless-deployment.md) - Backend-only deployment (no UI/AppSync/Cognito/WAF) — for API-only / pipeline integrations in Commercial regions or GovCloud
- [Configuration](./configuration.md) - Configuration and customization options
- [Configuration Profiles](./configuration-profiles.md) - Managing multiple named configurations and their revision history
- [IDP Configuration Best Practices](./idp-configuration-best-practices.md) - Guidelines for effective configuration design
- [JSON Schema Migration](./json-schema-migration.md) - JSON Schema format guide and legacy migration details
- [Web UI](./web-ui.md) - Web interface features and usage
- [IDP CLI](./idp-cli.md) - Command line interface for batch processing and evaluation workflows
- [IDP SDK](./idp-sdk.md) - Python SDK for programmatic access
- [Demo Videos](./demo-videos.md) - Comprehensive collection of feature demonstration videos
- [Troubleshooting](./troubleshooting.md) - Troubleshooting and performance guides
- [Error Analyzer](./error-analyzer.md) - AI-powered error diagnosis

## Processing Modes

- [BDA Mode Reference](./pattern-1.md) - Bedrock Data Automation (BDA) concepts and behavior
- [Pipeline Mode Reference](./pattern-2.md) - Textract + Bedrock classification and extraction
- [Discovery](./discovery.md) - Pattern-neutral discovery process and BDA blueprint automation

## Document Processing Features

- [Classification](./classification.md) - Customizing document classification
- [Extraction & Confidence](./extraction-and-confidence.md) - Extraction configuration, per-field confidence scoring, and bounding-box geometry (consolidates the former Extraction, Assessment, and Assessment Bounding Boxes guides)
- [Extraction Scaling Guide](./extraction-scaling-guide.md) - Simple vs advanced (agentic) mode limits by document/list size; where each hits limits and how to choose
- [Few-Shot Examples](./few-shot-examples.md) - Implementing few-shot examples for improved accuracy
- [Human-in-the-Loop Review](./human-review.md) - Human review workflows with built-in review system
- [Rule Validation](./rule-validation.md) - Business rule validation and compliance checking
- [Criteria Validation](./criteria-validation.md) - Document validation against dynamic business rules using LLMs
- [OCR Image Sizing Guide](./ocr-image-sizing-guide.md) - Optimizing image sizes for OCR processing
- [Languages](./languages.md) - Multi-language support

## Evaluation & Testing

- [Evaluation Framework](./evaluation.md) - Accuracy assessment system powered by Stickler
- [Evaluation Enhanced Reporting](./evaluation-enhanced-reporting.md) - Advanced evaluation reports with field-level comparisons
- [Test Studio](./test-studio.md) - Interactive testing with curated datasets

## AI Agents & Analytics

- [Agent Analysis](./agent-analysis.md) - Natural language analytics and data visualization feature
- [Agent Companion Chat](./agent-companion-chat.md) - Conversational agent for document analysis
- [Code Intelligence](./code-intelligence.md) - Chat bot for asking questions about the IDP code base and features
- [Knowledge Base](./knowledge-base.md) - Document knowledge base query feature
- [Custom MCP Agent](./custom-MCP-agent.md) - Integrating external MCP servers for custom tools and capabilities
- [MCP Server](./mcp-server.md) - Model Context Protocol integration for external applications

## Integration & Extensions

- [Post-Processing Lambda Hook](./post-processing-lambda-hook.md) - Custom downstream processing integration
- [Lambda Hook Inference](./lambda-hook-inference.md) - Custom LLM integration via Lambda hooks
- [Nova Fine-Tuning](./nova-finetuning.md) - Fine-tuning Amazon Nova models for IDP tasks
- [OpenAI GPT-5.x Models](./openai-models.md) - GPT-5.4 / GPT-5.5 / GPT-5.6 (Sol/Terra/Luna) via the bedrock-mantle Responses API: support matrix, limitations, regions, caching
- [xAI Grok Models](./grok-models.md) - Grok 4.6 via the Converse API (CRIS-only `us.`/`global.` IDs): support matrix, agentic extraction, reasoning effort, tier and caching caveats
- [Service Tiers](./service-tiers.md) - Configurable service tier options

## Monitoring & Operations

- [Monitoring](./monitoring.md) - Monitoring and logging capabilities
- [Reporting Database](./reporting-database.md) - Analytics database for evaluation metrics and metering data
- [Capacity Planning](./capacity-planning.md) - Performance optimization and resource scaling guidance
- [Circuit Breaker](./circuit-breaker.md) - Automatic protection from cascading failures during Bedrock outages
- [Cross-Account Bedrock](./cross-account-bedrock.md) - Route all Bedrock invocations through a centralized hub account via STS AssumeRole
- [Cost Calculator](./cost-calculator.md) - Framework for estimating solution costs

## Planning & Security

- [Well-Architected Framework Assessment](./well-architected.md) - Analysis based on AWS Well-Architected Framework
- [AWS Services & IAM Roles](./aws-services-and-roles.md) - AWS services used and IAM role requirements
- [API Gateway Hosting](./apigateway-hosting.md) - Serve the full Web UI from the existing API Gateway REST API (S3 proxy), within a VPC when combined with `ApiGatewayVisibility=PRIVATE` (alternative to CloudFront)
- [GovCloud Deployment](./govcloud-deployment.md) - Deploy to GovCloud with the full Web UI (`--govcloud`) or headless (`--headless`)
- [EU Region Model Support](./eu-region-model-support.md) - Model availability in EU regions

## Development Setup

- [Setup: Linux](./setup-development-env-linux.md) - Development environment setup for Linux
- [Setup: macOS](./setup-development-env-macos.md) - Development environment setup for macOS
- [Setup: WSL](./setup-development-env-WSL.md) - Development environment setup for Windows Subsystem for Linux
- [Using Notebooks](./using-notebooks-with-idp-common.md) - Guide for using Jupyter notebooks with the IDP Common Library

## Migration

- [Migration v0.4 to v0.5](./migration-v04-to-v05.md) - Upgrading from v0.4.x to v0.5.x (Unified Pattern)

### GovCloud

- [GovCloud Deployment Guide](./govcloud-deployment.md) - Deployment options (Web UI or headless), prerequisites, and deploy commands
- [GovCloud Architecture](./govcloud-architecture.md) - Services removed vs. retained, limitations, and workarounds
- [Batch Jobs REST API](./govcloud-batch-api.md) - API reference, authentication, and bastion tunnel setup
- [GovCloud Operations](./govcloud-operations.md) - Monitoring, troubleshooting, and best practices

## Blogs, Customer Stories & Research

- [Blogs, Customer Stories & Research](./references.md) - AWS blog deep-dives into the accelerator, customer references with real-world metrics, and peer-reviewed research papers

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
