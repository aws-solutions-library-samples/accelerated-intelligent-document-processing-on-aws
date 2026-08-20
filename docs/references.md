---
title: "Blogs, Customer Stories & Research"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Blogs, Customer Stories & Research

External publications about the GenAI Intelligent Document Processing (GenAIIDP)
Accelerator — AWS blog deep-dives into the solution's features, customer
references that share real-world results and metrics, and peer-reviewed research
that underpins the accelerator's approach.

## Feature Deep-Dives

AWS Machine Learning Blog posts that introduce and explain the accelerator and
its capabilities.

- **[Accelerate intelligent document processing with generative AI on AWS](https://aws.amazon.com/blogs/machine-learning/accelerate-intelligent-document-processing-with-generative-ai-on-aws/)**
  *(Aug 2025)* — The launch and overview post for the open-source accelerator.
  Covers the serverless architecture, the two runtime-switchable processing
  modes (Amazon Bedrock Data Automation and the Bedrock Pipeline mode),
  classification, extraction, human-in-the-loop review, and knowledge base
  integration, deployable via CloudFormation in ~15–20 minutes.

- **[Enhance document analytics with Strands AI agents for the GenAI IDP Accelerator](https://aws.amazon.com/blogs/machine-learning/enhance-document-analytics-with-strands-ai-agents-for-the-genai-idp-accelerator/)**
  *(Dec 2025)* — Introduces the Analytics Agent, which lets non-technical users
  query processed document data in natural language. Built on Strands Agents, it
  autonomously explores database schemas, generates and runs Athena SQL, executes
  Python in a secure AgentCore Code Interpreter sandbox, and returns
  visualizations. See also [Agent Analysis](./agent-analysis.md).

- **[Automate schema generation for intelligent document processing](https://aws.amazon.com/blogs/machine-learning/automate-schema-generation-for-intelligent-document-processing/)**
  *(May 2026)* — Introduces multi-document discovery, which generates extraction
  schemas from collections of unlabeled documents — removing the requirement to
  know your document classes up front. It clusters documents by type with visual
  embeddings (Cohere Embed v4 on Amazon Bedrock), then uses Strands Agents to
  analyze each cluster, generate JSON schemas, and reflect to catch overlaps.
  See also [Discovery](./discovery.md).

- **[Automate document processing with Quick Automate and the IDP Accelerator](https://aws.amazon.com/blogs/machine-learning/automate-document-processing-with-quick-automate-and-the-idp-accelerator/)**
  *(Aug 2026)* — A business-level case for pairing the accelerator with Amazon
  Quick Automate: the accelerator classifies and extracts, assesses results
  against expected schemas, and flags anomalies (missing fields, inconsistent
  figures, incomplete forms) for human review; Quick Automate's no-code visual
  workflow builder then chains the downstream decisions, API calls, and system
  actions. Framed as a mortgage-lending scenario for a fictional lender —
  document handling cut from 15–20 minutes to under 6 minutes per loan file
  ("up to 70 percent"), with the post explicitly labeling the company and its
  metrics illustrative rather than measured. The concrete integration mechanism
  is in the companion
  [Amazon Quick integration workshop](../workshop/amazon-quick-integration-workshop.md):
  deploy with `EnableMCP=true`, register the accelerator's AgentCore Gateway as
  an MCP integration in Amazon Quick over service-to-service OAuth, then drive
  `IDPTools__process` / `get_results` from a Quick workflow that writes the
  extracted fields to an Excel workbook in S3. See also
  [MCP Server](./mcp-server.md) and [Human-in-the-Loop Review](./human-review.md).

## Customer Stories

Reference deployments showing measurable accuracy, cost, and throughput results.

- **[How Myriad Genetics achieved fast, accurate, and cost-efficient document processing](https://aws.amazon.com/blogs/machine-learning/how-myriad-genetics-achieved-fast-accurate-and-cost-efficient-document-processing-using-the-aws-open-source-generative-ai-intelligent-document-processing-accelerator/)**
  *(Nov 2025)* — Genetic-testing provider Myriad Genetics, with the AWS
  Generative AI Innovation Center, raised classification accuracy from 94% to
  98%, cut costs 77% and processing time 80% (8.5 → 1.5 min/doc), and reached 90%
  extraction accuracy — projecting up to $132K in annual savings using Amazon
  Nova models.

- **[How Associa transforms document classification with the GenAI IDP Accelerator and Amazon Bedrock](https://aws.amazon.com/blogs/machine-learning/how-associa-transforms-document-classification-with-the-genai-idp-accelerator-and-amazon-bedrock/)**
  *(Feb 2026)* — Associa, North America's largest community management company
  (~7.5M homeowners), built a classification system on Amazon Nova Pro reaching
  95% accuracy at ~0.55 cents per document using a first-page-only OCR + image
  approach.

- **[How Ricoh built a scalable intelligent document processing solution on AWS](https://aws.amazon.com/blogs/machine-learning/how-ricoh-built-a-scalable-intelligent-document-processing-solution-on-aws/)**
  *(Mar 2026)* — Ricoh USA built a multi-tenant healthcare IDP solution
  (Amazon Textract + Amazon Bedrock) that cut client onboarding from weeks to
  days, reduced engineering hours per deployment by 90%+, held 98–99% accuracy,
  and scaled toward a projected 70,000 documents/month.

- **[Built Technologies builds an AI-powered document intelligence solution to power agents across real estate finance](https://aws.amazon.com/blogs/machine-learning/built-technologies-builds-an-ai-powered-document-intelligence-solution-on-aws-to-power-agents-across-real-estate-finance/)**
  *(Jul 2026)* — Built Technologies (processing $500B+ in projects), with AWS
  GenAIIC and AND Digital, built a reusable solution that classifies, extracts,
  and reasons over 250+ document types — collapsing 3–9 day workflows to minutes,
  with plans to scale to 20M documents/month at 95%+ confidence.

## Research Papers

Published and preprint work behind the accelerator's agentic, configuration
optimization, confidence-calibration, and document-splitting approaches.

- **[IDP Accelerator: Agentic Document Intelligence from Extraction to Compliance Validation](https://arxiv.org/abs/2602.23481)**
  *(ACL 2026)* — The research paper describing the accelerator itself: a
  four-part agentic framework spanning a segmentation benchmark/classifier, a
  multimodal extraction module, an MCP-compliant analytics module, and an
  LLM-driven rule-validation module — reporting 98% classification accuracy, 80%
  lower processing latency, and 77% lower operational cost in a healthcare
  deployment.

- **[IDP AutoOpt: Agent-Driven Optimization of Document Processing Pipeline Configurations](https://arxiv.org/abs/2607.26075)**
  *(arXiv preprint, Jul 2026)* — The research paper behind the
  [Auto Optimizer](./extensions/auto-optimizer.md): an autonomous LLM agent that
  discovers high-performing pipeline configurations (prompts, models, OCR
  settings, schemas) in a closed loop — scoring against a small labeled set,
  diagnosing field-level errors, proposing targeted edits, and re-evaluating —
  steered by human-authored domain skills. Reports matching or exceeding
  human-expert accuracy at equal or lower cost (90.2% vs 81.6% at 4.6x lower
  per-page cost on an extraction benchmark), cutting setup from weeks to under
  two hours. Also finds a hard agent-capability threshold below which
  optimization fails, and that curated domain skills beat raw source-code
  access.

- **[Can You Trust the Confidence? ConfBench for Vision-Language Models on Document Extraction](https://arxiv.org/abs/2608.01792)**
  *(arXiv preprint, Aug 2026)* — The research paper behind the
  [Test Set - ConfBench](./extensions/confbench-testset.md) benchmark: the first
  calibration-specific benchmark for key information extraction, built by running
  20 controlled degradation pipelines over a document collection to produce 1,346
  variants and 70K+ entity-level evaluations spanning the full accuracy range —
  the low-accuracy region that clean benchmarks leave too sparse to measure
  calibration. Evaluates four proprietary and three open-weight VLMs across
  verbalized and log-probability confidence estimation and three input
  modalities, finding that OCR+Image yields the most accurate confidence, that
  model capability (not parameter count) predicts confidence quality, that
  calibration ranges from near-perfect to severely overconfident but responds to
  per-model post-hoc correction, and that log-probability with first-token
  aggregation beats mean-token and margin aggregation. Also introduces ECARB, a
  review-budget metric that translates discriminative gains into operational
  savings. See also [Assessment](./assessment.md).

- **[DocSplit: A Comprehensive Benchmark Dataset and Evaluation Approach for Document Packet Recognition and Splitting](https://www.amazon.science/publications/docsplit-a-comprehensive-benchmark-dataset-and-evaluation-approach-for-document-packet-recognition-and-splitting)**
  *(ACL 2026)* — Introduces the first benchmark and evaluation metrics for
  document packet splitting (detecting boundaries, classifying types, and
  preserving page order across multi-page packets), spanning five datasets of
  varying complexity and revealing significant performance gaps for multimodal
  LLMs on the task.
