---
title: "IDP Monitor"
---
# IDP Monitor

**IDP Monitor** is an AI-powered operations dashboard for your IDP Accelerator deployment. An AI Assistant answers plain-language questions about your pipeline — failures, cost, throughput, latency — by translating them into SQL against your reporting data and assembling a live dashboard view on the fly. Scheduled Agents run those same questions automatically on a cron you choose and notify you with the results. Underneath the AI layer, a library of configurable widgets shows what your document pipeline is doing right now and what it has cost you, on one screen inside the IDP web UI.

## What it does

Once an IDP Accelerator pipeline is in production, the questions change. Is throughput keeping up? Which document type is failing, and why? What did yesterday cost, and which configuration version or model drove it? Which documents came back with low confidence and are waiting for human review? Answering these from raw CloudWatch logs, Step Functions histories and reporting tables is slow, and it gets slower as volume grows. IDP Monitor puts the answers on one screen — ask a question in plain language and get an instant answer with the relevant widgets, or browse the dashboard directly.



https://github.com/user-attachments/assets/052ce85e-ad02-4169-ba9f-0f8a7ebd0403



## AI Assistant and Insights

An **AI Assistant** panel opens from the toolbar. Ask a plain-language question:

- *What failed last week?*
- *Show me cost by document type for the last 30 days.*
- *Compare throughput this week vs last week.*

A purpose-built analytics agent translates the question into SQL against your Athena reporting tables, answers in text, and assembles a layout: one to five dashboard widgets relevant to the question, plus charts or tables the agent generates on the fly. The result appears in an **AI Insights** tab with the answer at the top, live widgets underneath, and suggested follow-up questions you can click. If you like the view, save it as a dashboard of your own. When the assistant detects a scheduling intent ("every Monday morning, summarise last week's failures"), it shows a confirmation card and you can create the schedule directly from the chat.

The agent only selects and arranges widgets that already exist; it does not invent charts or numbers. It has read-only access to the reporting database and cannot change your pipeline configuration.

## Scheduled Agents and Notifications

Any question you can ask the AI Assistant can also be run on a schedule. The **Scheduled Agents** page shows a table of your schedules with actions: activate or pause, run immediately, edit, or delete. Start from a preset — Daily Health Report, Failure Analysis (every 4 hours), Weekly Cost Report, Hourly Throughput Check, Confidence Monitoring (weekdays), or Daily Volume Summary — or write your own question with a custom cron expression. Each schedule becomes an Amazon EventBridge rule in your account. When it fires, the agent runs the question, stores the result, and raises an in-app notification.

A **notification bell** in the toolbar shows a badge with the unread count. Open it to see results from scheduled agents; click a result to read the answer and the dashboard view it produced. Notifications can be marked read, deleted, or cleared in bulk. Each schedule keeps a run history, and you can trigger an immediate run at any time. Scheduled agents and notifications are gated by a feature toggle in Settings.

## Dashboard Widgets

IDP Monitor ships with a library of widgets you can turn on and arrange per dashboard.

**Operations widgets**

- **AI Summary** — the top widget on every built-in dashboard; generates severity-ranked recommendation cards from the current data window, each linking to the relevant widget. Per dashboard you can set the model, creativity level, custom instructions, and the number of insights.
- **Key Metrics** — four KPI tiles: documents processed with success rate, total pages with pages per document, Bedrock tokens (input and output), and total cost with cost per document and cost per page
- **Document Status** — in-progress documents right now, plus completed and failed volume over time
- **Processing Speed** — processing time per pipeline stage with P50 and P90 percentiles
- **Service Performance** — per-service latency and error rates across the pipeline
- **Document Failures** — recent failures with error category, error message, and a link to the document
- **Throttle events** — service throttling and rate-limit events across Amazon Bedrock, Amazon Textract, AWS Lambda and Amazon DynamoDB, with severity
- **Confidence Alerts** — documents whose extraction confidence fell below your threshold
- **Human-in-the-Loop** — the size and age of the human review queue
- **Workflows** — workflow concurrency and capacity utilization
- **Document Distribution** — classification distribution, including documents that mix several types
- **Configuration Context** — which pipeline configuration versions produced the documents in the window, so you can correlate a change in behaviour with a configuration deployment

**Cost widgets**

- **Cost Trends** — spend over time
- **Cost Distribution** — by document type and by configuration version
- **Pipeline Stage Cost** — cost broken down by pipeline stage (OCR, classification, extraction, assessment, and so on)
- **Service Cost** — what the pipeline's own Lambda functions and control-plane agents cost, separately from per-document inference
- **High-Cost Documents** — a ranked list you can open document by document

Click a slice of the document-type chart or a row in the failures table to open the matching documents in a detail view.

## Dashboards

IDP Monitor ships with five built-in dashboards. **Dashboard** is a combined operations-and-cost view. **Daily Operations** and **Daily Cost** default to the last 24 hours; **Monthly Operations** and **Monthly Cost** default to the last 30 days. Each opens as a tab on the Monitoring page. You can create additional dashboards from Settings — pick widgets, set a default time range and refresh interval, and configure the AI Summary per dashboard. You can also save a dashboard directly from the AI Assistant: ask a question, and if you like the widget layout it assembles, click **Save Dashboard** to keep it. Saved dashboards appear in a toolbar dropdown for quick access.

On any dashboard you can:

- change the **time range** with presets from the last two hours to the last 30 days, or pick a custom start and end
- **refresh** manually or set an auto-refresh interval per dashboard
- open the **AI Assistant** to ask a question about the data you are looking at

## Availability

IDP Monitor is an extension to the IDP Accelerator, available as a subscription on [AWS Marketplace](https://aws.amazon.com/marketplace/pp/prodview-guhlipxo6hpl2). It is in **beta** and **free of charge during the beta**: subscribing means accepting the beta licence terms shown on the listing, and it is labelled **Monitor (Beta)** in the IDP web UI until it reaches general availability. It still runs entirely in your own account, so the AWS services it uses — Athena, DynamoDB, CloudWatch, Bedrock for the AI features — are billed to you as usual.

It is published for the same Regions as the accelerator's own templates: **us-east-1**, **us-west-2**, and **eu-central-1**. Your IDP Accelerator stack has to run in one of them; an extension page in any other Region says so instead of offering an install.

Once installed, it appears under **Extensions** in the IDP web UI navigation and adds a **Monitoring** page.

## Getting access

1. **Deploy the IDP Accelerator** if you haven't already. The extension installs into a host stack you run, so it has nothing to attach to on its own. See [Quick Start](../quick-start.md).
2. **Subscribe on [AWS Marketplace](https://aws.amazon.com/marketplace/pp/prodview-guhlipxo6hpl2)**, where you accept the beta licence terms and the AWS Customer Agreement. There is no charge for the extension during the beta. Subscribe with the same AWS account your IDP Accelerator stack runs in. A subscription held by another account in your organization isn't visible to a member account's stack.
3. **Install it from the IDP web UI.** Sign in as an `Admin`, open **Monitor (Beta)** under **Extensions**, and choose **Launch Stack**. Full walkthrough: [After Subscribing on AWS Marketplace](../marketplace-subscription-next-steps.md).



https://github.com/user-attachments/assets/da06a1b9-9eb5-4661-909b-0c774d9b2994



Manage or cancel the subscription any time from the [AWS Marketplace subscriptions console](https://console.aws.amazon.com/marketplace/home#/subscriptions). Cancelling does not delete the extension's CloudFormation stack. Delete that separately if you want its resources removed; the dashboard configuration and schedule tables are removed with the stack.

For questions about IDP Monitor, reach out to your AWS account team.
