---
title: "Hands-on Workshops"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Hands-on Workshops

Two hands-on workshops cover the GenAI IDP Accelerator. They are delivered
differently and teach different things, so pick the one that matches what you
want to do.

| | [GenAI IDP Workshop](#genai-idp-workshop-aws-workshop-studio) | [Amazon Quick Integration Workshop](#amazon-quick-integration-workshop-self-guided) |
|---|---|---|
| **What it teaches** | The accelerator end to end: both processing modes, configuration, quality controls and the built-in AI features | One integration: driving the accelerator from an Amazon Quick workflow over MCP |
| **Where it runs** | [AWS Workshop Studio](https://catalog.workshops.aws/workshops/c384d63d-3090-44c9-a639-e06dd6d9164f), at an AWS-hosted event (recommended), or self-paced | Your own AWS account |
| **AWS account** | At an event: a temporary account for each participant. Self-paced: your own | Yours, with administrator access |
| **Deployment** | At an event: pre-deployed before it starts. Self-paced: you deploy it (~30 minutes) | You deploy the stack yourself (~10–15 minutes) |
| **Format** | Instructor-led (customer enablement session or workshop, Immersion Day, GameDay), or self-paced | Self-guided, following a guide in this repository |
| **Best for** | Teams evaluating or adopting the accelerator | Builders who already know the accelerator and want to connect it to Amazon Quick |

## GenAI IDP Workshop (AWS Workshop Studio)

The GenAI IDP Workshop covers the full solution and runs on
[AWS Workshop Studio](https://catalog.workshops.aws/workshops/c384d63d-3090-44c9-a639-e06dd6d9164f),
the AWS platform for hands-on events. At an AWS-hosted event, Workshop Studio
gives every participant a temporary AWS account with the accelerator already
deployed, so the session goes straight to working with the solution. Participants at an event do not need an AWS
account of their own, and nothing is deployed to or billed against their accounts.

### What you will do

- **Getting started:** process a lending package end to end with the
  `lending-package-sample` configuration and review the results in the Web UI.
- **BDA mode:** managed processing with Amazon Bedrock Data Automation blueprints.
- **Pipeline mode:** modular OCR → classification → extraction with Amazon
  Textract and Amazon Bedrock, and how to customize each step.
- **Assessment, evaluation and human-in-the-loop review:** confidence scoring,
  accuracy against a baseline, and the built-in review workflow.
- **Knowledge Base:** natural-language questions over your processed documents.
- **Agent Companion Chat:** AI-assisted analytics and troubleshooting.
- **Test Studio:** benchmark and compare configurations against ground truth.

### How to attend

The recommended way to take the workshop is at an AWS-hosted event, where Workshop
Studio provides the accounts and pre-deploys the solution. The event can be an
enablement session or hands-on workshop for your team, an Immersion Day, or a
GameDay.

To arrange a hands-on session or enablement workshop for your team, contact your
AWS account team or Solutions Architect.

**Self-paced option:** you can also work through the workshop on your own, in
your own AWS account. Its deployment section includes a self-paced path that
deploys the stack for you to use (~30 minutes). You pay for the resources you
deploy, so delete the stack when you are finished.

## Amazon Quick Integration Workshop (self-guided)

The [Amazon Quick integration workshop](../workshop/amazon-quick-integration-workshop.md)
is a step-by-step guide kept in this repository. Run it in your own AWS account
whenever you like. You deploy the accelerator with `EnableMCP=true`, register its
MCP endpoint as an integration in Amazon Quick, and build a Quick workflow that
processes a loan package and writes the extracted fields to an Excel workbook in S3.

It assumes you are comfortable deploying the accelerator. If you are new to it,
start with [Deployment](./deployment.md) or the Workshop Studio workshop above.
Related page: [MCP Server](./mcp-server.md).
