# Gen AI Intelligent Document Processing (GenAIIDP)

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

<!-- Internal GitLab pipeline badge (visible on gitlab.aws.dev, not accessible on public GitHub mirror) -->
[![pipeline status](https://gitlab.aws.dev/genaiic-reusable-assets/engagement-artifacts/genaiic-idp-accelerator/badges/develop/pipeline.svg)](https://gitlab.aws.dev/genaiic-reusable-assets/engagement-artifacts/genaiic-idp-accelerator/-/commits/develop)

**Questions?** [![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws)

📖 **[Browse the Documentation Site](https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/)** — searchable, with sidebar navigation

## Table of Contents

- [Gen AI Intelligent Document Processing (GenAIIDP)](#gen-ai-intelligent-document-processing-genaiidp)
  - [Table of Contents](#table-of-contents)
  - [Introduction](#introduction)
  - [Alternative Implementations](#alternative-implementations)
  - [Key Features](#key-features)
  - [Architecture Overview](#architecture-overview)
  - [Quick Start](#quick-start)
    - [Processing Your First Document](#processing-your-first-document)
      - [Method 1: Web UI (Interactive)](#method-1-web-ui-interactive)
      - [Method 2: Direct S3 Upload (Simple)](#method-2-direct-s3-upload-simple)
      - [Method 3: IDP CLI (Batch/Programmatic)](#method-3-idp-cli-batchprogrammatic)
  - [Updating an Existing Deployment](#updating-an-existing-deployment)
  - [Detailed Documentation](#detailed-documentation)
    - [Core Documentation](#core-documentation)
    - [Processing Modes](#processing-modes)
    - [Python Development](#python-development)
    - [Planning \& Operations](#planning--operations)
  - [Contributing](#contributing)
  - [License](#license)

## Introduction

A scalable, serverless solution for automated document processing and information extraction using AWS services. This system combines OCR capabilities with generative AI to convert unstructured documents into structured data at scale.


https://github.com/user-attachments/assets/fc2652b5-a9cc-42d7-9975-887c8320a2f5


Concierge support for customization, deployment, and integration of production use cases is available through [AWS Professional Services](https://aws.amazon.com/professional-services/).

## Alternative Implementations

**Prefer AWS CDK?** This solution is also available as [GenAI IDP Accelerator for AWS CDK](https://github.com/cdklabs/genai-idp), providing the same functional capabilities through AWS CDK constructs for customers who prefer Infrastructure-as-Code with CDK.

**Prefer Terraform?** This solution is also available as [GenAI IDP Terraform](https://github.com/awslabs/genai-idp-terraform), providing the same functional capabilities as a Terraform module that integrates with existing infrastructure and supports customization through module variables.

## Key Features

- **Serverless Architecture**: Built entirely on AWS serverless technologies including Lambda, Step Functions, SQS, and DynamoDB
- **Modular, pluggable patterns**: Pre-built processing patterns using state-of-the-art models and AWS services
- **Command Line Interface**: Programmatic batch processing with evaluation framework, analytics integration, and interactive Agent Chat
- **Advanced Classification**: Support for page-level and holistic document packet classification
- **Few Shot Example Support**: Improve accuracy through example-based prompting
- **Custom Business Logic Integration**: Inject custom prompt generation logic via Lambda functions for specialized document processing
- **High Throughput Processing**: Handles large volumes of documents through intelligent queuing
- **Built-in Resilience**: Comprehensive error handling, retries, and throttling management
- **Cost Optimization**: Pay-per-use pricing model with built-in controls
- **Comprehensive Monitoring**: Rich CloudWatch dashboard with detailed metrics and logs
- **Web User Interface**: Modern UI for inspecting document workflow status and results
- **Configuration Versioning**: Support for multiple configuration versions with version-specific processing and test comparison
- **Document Versioning**: Every processing run of a document is retained as an immutable version — view, compare, download, and manage prior outputs (see [document-versions.md](./docs/document-versions.md))
- **Human-in-the-Loop (HITL)**: Built-in review system for human validation workflows
- **AI-Powered Evaluation**: Framework to assess accuracy against baseline data
- **Extraction Confidence Assessment**: LLM-powered assessment of extraction confidence with multimodal document analysis
- **Document Knowledge Base Query**: Ask questions about your processed documents
- **IDP Accelerator Help Chat Bot**: Ask questions about the IDP code base or features
- **MCP Integration**: Model Context Protocol integration enabling external applications like Amazon Quick Suite to access IDP data and analytics through AWS Bedrock AgentCore Gateway

## Architecture Overview

![Architecture Diagram](./images/IDP.UnifiedPatterns.drawio.png)

The solution uses a modular architecture with nested CloudFormation stacks to support multiple document processing patterns while maintaining common infrastructure for queueing, tracking, and monitoring.

The unified pattern supports two processing modes, controlled by the `use_bda` configuration flag:
- **Pipeline mode** (default): OCR → Bedrock Classification (page-level or holistic) → Bedrock Extraction → Assessment → Rule Validation → Summarization
- **BDA mode**: End-to-end processing with Bedrock Data Automation (BDA) → Rule Validation → Summarization

## Quick Start

To quickly deploy the GenAI-IDP solution in your AWS account:

1. Log into the [AWS console](https://console.aws.amazon.com/)
2. Choose the **Launch Stack** button below for your desired region:

| Region name           | Region code | Launch                                                                                                                                                                                                                                                                                                                                                                      |
| --------------------- | ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| US West (Oregon)      | us-west-2   | [![Launch Stack](https://cdn.rawgit.com/buildkite/cloudformation-launch-stack-button-svg/master/launch-stack.svg)](https://us-west-2.console.aws.amazon.com/cloudformation/home?region=us-west-2#/stacks/create/review?templateURL=https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main.yaml&stackName=IDP) |
| US East (N.Virginia)      | us-east-1   | [![Launch Stack](https://cdn.rawgit.com/buildkite/cloudformation-launch-stack-button-svg/master/launch-stack.svg)](https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main.yaml&stackName=IDP) |
| EU Central (Frankfurt)      | eu-central-1   | [![Launch Stack](https://cdn.rawgit.com/buildkite/cloudformation-launch-stack-button-svg/master/launch-stack.svg)](https://eu-central-1.console.aws.amazon.com/cloudformation/home?region=eu-central-1#/stacks/create/review?templateURL=https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main.yaml&stackName=IDP) |

3. When the stack deploys for the first time, you'll receive an email with a temporary password to access the web UI
4. Use this temporary password for your first login to set up a permanent password

### Processing Your First Document

After deployment, choose the processing method that fits your use case:

#### Method 1: Web UI (Interactive)

1. Open the Web UI URL from CloudFormation stack Outputs
2. Log in and click "Upload Document"
3. Upload a sample document:
   - [samples/lending_package.pdf](./samples/lending_package.pdf)
4. Monitor processing and view results in the dashboard

#### Method 2: Direct S3 Upload (Simple)

1. Upload to the InputBucket (URL in CloudFormation Outputs)
2. Monitor via Step Functions console
3. Results appear in OutputBucket automatically

#### Method 3: IDP CLI (Batch/Programmatic)

For batch processing, automation, or evaluation workflows:

```bash
# Install CLI
cd lib/idp_cli_pkg && pip install -e .

# Process documents
idp-cli run-inference \
    --stack-name <your-stack-name> \
    --dir ./samples/ \
    --monitor

# Download results
idp-cli download-results \
    --stack-name <your-stack-name> \
    --batch-id <batch-id> \
    --output-dir ./results/
```

**See [IDP CLI Documentation](./docs/idp-cli.md)** for:
- CLI-based stack deployment and updates
- Batch document processing
- Complete evaluation workflows with baselines
- Athena and Agent Analytics integration
- CI/CD integration examples

See the [Deployment Guide](./docs/deployment.md#testing-the-solution) for more detailed testing instructions.

IMPORTANT: If you have not previously done so, you must [request access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html) to the following Amazon Bedrock models:
- Amazon: All Nova models, plus Titan Text Embeddings V2
- Anthropic: Claude 3.x models, Claude 4.x models

## Updating an Existing Deployment

To update an existing GenAIIDP stack to a new version:

1. Navigate to CloudFormation in the AWS Management Console
2. Select your existing stack
3. Click "Update"
4. Select "Replace current template"
5. Enter the template URL: 
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main.yaml`
6. Follow the prompts to update your stack, reviewing any parameter changes
7. For detailed instructions, see the [Deployment Guide](./docs/deployment.md#updating-an-existing-stack)

For testing, use these sample files:
   - Use [samples/lending_package.pdf](./samples/lending_package.pdf) for both Pipeline and BDA modes

For detailed deployment and testing instructions, see the [Deployment Guide](./docs/deployment.md).


## Detailed Documentation

> 📖 **[Browse all documentation on the GenAIIDP Docs Site](https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/)** — full-text search, sidebar navigation, and organized by topic.

### Core Documentation

- [Architecture](./docs/architecture.md) - Detailed component architecture and data flow
- [Demo Videos](./docs/demo-videos.md) - Comprehensive collection of feature demonstration videos
- [Deployment](./docs/deployment.md) - Build, publish, deploy, and test instructions
- [Headless Deployment](./docs/headless-deployment.md) - Backend-only deployment (no UI/AppSync/Cognito/WAF) for API-only use cases
- [GovCloud Deployment](./docs/govcloud-deployment.md) - Deploy to GovCloud with the full Web UI (`--govcloud`) or headless (`--headless`)
- [IDP CLI](./docs/idp-cli.md) - Command line interface for batch processing, evaluation workflows, and interactive Agent Chat
- [Web UI](./docs/web-ui.md) - Web interface features and usage
- [Document Versions](./docs/document-versions.md) - Retain, view, compare, and manage every processing run of a document
- [Agent Analysis](./docs/agent-analysis.md) - Natural language analytics and data visualization feature
- [Custom MCP Agent](./docs/custom-MCP-agent.md) - Integrating external MCP servers for custom tools and capabilities
- [Configuration](./docs/configuration.md) - Configuration and customization options
- [JSON Schema Migration](./docs/json-schema-migration.md) - JSON Schema format guide and legacy migration details
- [Discovery](./docs/discovery.md) - Pattern-neutral discovery process and BDA blueprint automation
- [Classification](./docs/classification.md) - Customizing document classification
- [Extraction](./docs/extraction.md) - Customizing information extraction
- [Human-in-the-Loop Review](./docs/human-review.md) - Human review workflows with built-in review system
- [Assessment](./docs/assessment.md) - Extraction confidence evaluation using LLMs
- [Rule Validation](./docs/rule-validation.md) - Business rule validation and compliance checking
- [Evaluation Framework](./docs/evaluation.md) - Accuracy assessment system with analytics database and reporting
- [MLflow Experiment Tracking](./docs/mlflow-integration.md) - Optional MLflow integration for tracking metrics, model parameters, and prompts across test runs
- [Knowledge Base](./docs/knowledge-base.md) - Document knowledge base query feature
- [Monitoring](./docs/monitoring.md) - Monitoring and logging capabilities
- [IDP Accelerator Help Chat Bot](./docs/code-intelligence.md) - Chat bot for asking question about the IDP code base and features
- [MCP Integration](./docs/mcp-integration.md) - Model Context Protocol integration for external applications like Amazon Quick Suite
- [Reporting Database](./docs/reporting-database.md) - Analytics database for evaluation metrics and metering data
- [Troubleshooting](./docs/troubleshooting.md) - Troubleshooting and performance guides

### Processing Modes

- [Architecture](./docs/architecture.md) - Unified pattern with BDA and Pipeline processing modes
- [BDA Mode Reference](./docs/pattern-1.md) - Bedrock Data Automation (BDA) concepts and behavior
- [Pipeline Mode Reference](./docs/pattern-2.md) - Textract + Bedrock classification and extraction
- [Few-Shot Examples](./docs/few-shot-examples.md) - Implementing few-shot examples for improved accuracy

### Python Development

- [Using Notebooks with IDP Common Library](./docs/using-notebooks-with-idp-common.md) - Guide for using and creating Jupyter notebooks to experiment with the IDP Common Library
- [IDP Common Package](./lib/idp_common_pkg/README.md) - Documentation for the core library powering the accelerator

### Planning & Operations

- [Capacity Planning](./docs/capacity-planning.md) - Comprehensive capacity planning, performance optimization, and resource scaling guidance
- [Well-Architected Framework Review](./docs/well-architected.md) - What the solution implements per pillar, plus a checklist template you fill in for your own deployment
- [AWS Services & IAM Roles](./docs/aws-services-and-roles.md) - AWS services used and IAM role requirements
- [Cost Calculator](./docs/cost-calculator.md) - Framework for estimating solution costs

## Contributing

We welcome contributions to the GenAI Intelligent Document Processing accelerator! Whether you're fixing bugs, improving documentation, or proposing new features, your contributions are appreciated.

Please refer to our [Contributing Guide](./CONTRIBUTING.md) for detailed information on:

- Setting up your development environment
- Project structure
- Making and testing changes
- Pull request process
- Coding standards
  - Python code uses `ruff` for linting
  - UI code uses ESLint (`npm run lint` to verify)
- Documentation requirements
- Issue reporting guidelines

Thank you to everyone who has contributed to making this project better!

## License

This project is licensed under the terms specified in the LICENSE file.
