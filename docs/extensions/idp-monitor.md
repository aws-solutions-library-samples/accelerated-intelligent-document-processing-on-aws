---
title: "IDP Monitor"
---
# IDP Monitor

**IDP Monitor** is an operations dashboard for your IDP Accelerator deployment. It shows what your document pipeline is doing right now and what it has cost you, on one screen inside the IDP web UI: document volume and status, per-stage latency, failures with error categories, throttling, low-confidence documents, the human review queue, and cost broken down by document type, pipeline stage, configuration version and model. An AI Assistant answers plain-language questions about the same data, assembles a view of the relevant widgets on the fly, and can run those questions on a schedule and notify you with the results.

## What it does

Once an IDP Accelerator pipeline is in production, the questions change. Is throughput keeping up? Which document type is failing, and why? What did yesterday cost, and which configuration version or model drove it? Which documents came back with low confidence and are waiting for human review? Answering these from raw CloudWatch logs, Step Functions histories and reporting tables is slow, and it gets slower as volume grows. IDP Monitor puts the answers on one screen, built from a library of widgets you can turn on and arrange per dashboard.

**Operations widgets**

- **Key metrics** — documents processed, success rate, total cost, and cost per document for the selected window
- **Document status** — in-progress documents right now, plus completed and failed volume over time
- **Latency** — processing time per pipeline stage with P50 / P90 / P99 percentiles
- **Failures** — recent failures with error category, error message, and a link to the document
- **Throttle events** — service throttling and rate-limit events across Amazon Bedrock, Amazon Textract, AWS Lambda and Amazon DynamoDB, with severity
- **Confidence alerts** — documents whose extraction confidence fell below your threshold
- **Human-in-the-loop** — the size and age of the human review queue
- **Concurrency** — workflow concurrency and capacity utilization
- **Document types** — classification distribution, including documents that mix several types
- **Configuration context** — which pipeline configuration versions produced the documents in the window, so you can correlate a change in behaviour with a configuration deployment

**Cost widgets**

- **Cost trends** — spend over time
- **Cost distribution** — by document type, by pipeline stage (OCR, classification, extraction, assessment, and so on), and by configuration version
- **Control-plane cost** — what the pipeline's own Lambda functions and control-plane agents cost, separately from per-document inference
- **Most expensive documents** — a ranked list you can open document by document

Every chart drills down to the documents behind it. Click a slice of the document-type chart, a bar in the cost distribution, or a row in the failures table to open the matching documents in a detail view.

## How it works

IDP Monitor installs into your existing IDP Accelerator stack as an extension. It deploys a small HTTP API and a Lambda function that read the stack's own data:

- **Amazon Athena** over the accelerator's [reporting database](../reporting-database.md) for everything historical: volume, latency, cost, token usage and document-type distribution. Queries run against hourly and daily rollup tables rather than the raw metering records, so the dashboard stays fast at high document volumes without adding load to your processing path.
- **Amazon DynamoDB** (the accelerator's tracking table) for in-flight state and recent failures.
- **Amazon CloudWatch** metrics and **AWS X-Ray** traces for throttling, concurrency and per-stage latency.
- **Amazon Bedrock** for the AI Summary widget and the AI Assistant.

The API is protected by the same Amazon Cognito user pool as the rest of the IDP web UI, so anyone who can sign in to your accelerator can open the Monitoring page. Everything runs inside your own AWS account and Region against your own stack's data. No document content, metrics or configuration leaves it.

## Dashboards

IDP Monitor ships with five built-in dashboards. **Dashboard** is a combined operations-and-cost view. **Daily Operations** and **Daily Cost** default to the last 24 hours; **Monthly Operations** and **Monthly Cost** default to the last 30 days. Each opens as a tab on the Monitoring page.

For any dashboard you can:

- change the **time range** with presets from the last hour to the last 30 days, or pick a custom start and end
- turn individual **widgets** on or off and reorder them
- set an **auto-refresh interval**, or refresh on demand
- choose the **AI Summary** settings for that dashboard only (see below)

Views you build yourself, and views the AI Assistant assembles for you, can be saved as named dashboards of your own. Dashboard settings are stored per user in the browser and in a versioned configuration store (see [Configuration versions](#configuration-versions)).

## AI Summary

Each dashboard can include a collapsible **AI Summary** widget. When you expand it, the backend gathers the current window's metrics from Athena, CloudWatch and DynamoDB in parallel and asks an Amazon Bedrock model for a short health narrative: a handful of insights grouped into categories such as failures, cost, throughput and health, each grounded in a specific number from the dashboard.

The widget is collapsed by default and no Bedrock call is made until you expand it, so a dashboard you never expand costs nothing extra. Per dashboard you can set the **model**, the **creativity level**, **custom instructions** for what to emphasise, the **summary categories** to organise around, and the **minimum and maximum number of insights**. Pressing **Refresh** regenerates the summary along with the widgets.

## AI Assistant and AI Insights

The **AI Assistant** panel turns the dashboard into a conversation. Ask a question in plain language, for example:

- *Why did cost go up last week?*
- *Which document types are throttling right now?*
- *Show me failures for W-2 forms in the last 24 hours.*
- *How much did the `v12` configuration cost per page compared with `v11`?*

A purpose-built analytics agent running on Amazon Bedrock translates the question into SQL against your Athena reporting tables, answers in text, and returns a layout directive: the one to five dashboard widgets most relevant to the question, and a time range if the question implied one. The Monitoring page renders that layout in an **AI Insights** tab, with the answer at the top, live widgets underneath, and suggested follow-up questions you can click. If you like the view, save it as a dashboard of your own.

The agent only selects and arranges widgets that already exist; it does not invent charts or numbers. It has read-only access to the reporting database and cannot change your pipeline configuration.

## Scheduled agents and notifications

Any question you can ask the AI Assistant can also be run on a schedule. When the assistant recognises a scheduling intent ("every Monday morning, summarise last week's failures"), it shows a confirmation card with the parsed schedule, and you can create the schedule from there, run the question once instead, or open the **Scheduled Agents** page to adjust the details.

On the Scheduled Agents page you can also start from a preset (Daily Health Report, Weekly Cost Report, Hourly Throughput Check, Daily Volume Summary), pick a cron preset such as daily at 9 AM UTC or weekly on Monday, or enter your own cron expression. Each schedule becomes an Amazon EventBridge rule in your account. When it fires, the agent runs the question, stores the result in the accelerator's reporting bucket, and raises an in-app notification. The **notification bell** in the Monitoring page shows unread results; open one to read the answer and the view it produced. Each schedule keeps a run history, and you can run a schedule immediately, pause it, or delete it at any time. There is a per-user limit on the number of active schedules.

## Configuration versions

Dashboard configuration (which widgets are enabled, default time ranges, refresh intervals, AI Summary settings) is stored as numbered versions in a DynamoDB table owned by the extension. Every version has a visibility:

- **Private** versions are visible only to the user who created them.
- **Global** versions are visible to everyone who can sign in, so a team can share a curated setup.

Each user chooses their own active version; the shipped default (`v1`) is the fallback for anyone who has not picked one. From the **Settings** page you can create a version from the current state, edit or delete versions you have access to, activate one, and **compare** any two versions side by side. If the **Export / Import** option is enabled, configurations can also be exported to and imported from JSON files, which is the simplest way to move a dashboard setup between environments.

## Relationship to the built-in monitoring

The IDP Accelerator already ships a CloudWatch dashboard and alarms for its own Lambda functions and workflows; see [Monitoring](../monitoring.md). IDP Monitor does not replace that. It adds the document-centric view (which document types, which configuration versions, which documents, at what cost) that CloudWatch service metrics cannot give you, and it puts that view inside the IDP web UI next to the documents themselves. Both read from the same underlying data, and the [reporting database](../reporting-database.md) that IDP Monitor queries is the same one available to you through Athena.

## Availability

IDP Monitor is a paid extension to the IDP Accelerator, available as a subscription on AWS Marketplace.

It is published for the same Regions as the accelerator's own templates: **us-east-1**, **us-west-2**, and **eu-central-1**. Your IDP Accelerator stack has to run in one of them; an extension page in any other Region says so instead of offering an install.

Once installed, it appears under **Extensions** in the IDP web UI navigation and adds a **Monitoring** page.

## Getting access

1. **Deploy the IDP Accelerator** if you haven't already. The extension installs into a host stack you run, so it has nothing to attach to on its own. See [Quick Start](../quick-start.md).
2. **Subscribe on AWS Marketplace**, where you accept pricing, the licence terms, and the AWS Customer Agreement. Subscribe with the same AWS account your IDP Accelerator stack runs in. A subscription held by another account in your organization isn't visible to a member account's stack.
3. **Install it from the IDP web UI.** Sign in as an `Admin`, open **IDP Monitor** under **Extensions**, and choose **Launch Stack**. Full walkthrough: [After Subscribing on AWS Marketplace](../marketplace-subscription-next-steps.md).

Manage or cancel the subscription any time from the [AWS Marketplace subscriptions console](https://console.aws.amazon.com/marketplace/home#/subscriptions). Cancelling does not delete the extension's CloudFormation stack. Delete that separately if you want its resources removed; the dashboard configuration and schedule tables are removed with the stack.

For questions about IDP Monitor, reach out to your AWS account team.
