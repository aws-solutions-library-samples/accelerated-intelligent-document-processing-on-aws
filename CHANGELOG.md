Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Changelog

## [0.6.7]

### Added

- **Extraction: a section holding several documents of the same class can now extract all of them.** Set `x-aws-idp-multi-instance: true` on a class and its effective schema becomes a list of that class, so `inference_result` holds `{"instances": [ … ]}` — one entry per document found — while you keep authoring the class as one record; it works in both Simple and Advanced (agentic) mode. A section can also report how many documents it found, and `extraction.multi_instance_detection.enabled` (**Simple extraction only**) asks the model that question directly, so the loss is visible even when the flag is off. In the Web UI both are a single **Documents per section** control under **Configuration → Document Schema**, including a picker for a class that already lists its records (`x-aws-idp-instance-array`). ⚠️ Turning the flag on requires migrating that class's evaluation baselines — see **Changed**. See [extraction and confidence](docs/extraction-and-confidence.md) ([#715](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/715), [#753](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/753), [#565](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/565), [#772](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/772)).

- **Extraction: simple mode now coerces and validates its output.** It did a raw `json.loads` and passed whatever came back downstream, so a wrong type or a non-ISO date reached storage unchallenged; the path most deployments run now gets the same guarantee Advanced extraction has had. Deterministic repair runs first without a model call (currency and thousands separators in both conventions, named-month and unambiguous numeric dates), then full JSON-Schema validation with optional escalation. ⚠️ **Coercion is on by default and changes stored values** — `"$1,234.00"` is now stored as `1234.0`, so a re-run on this release can differ from the same document's earlier output. Both halves are individually switchable under **Configuration → Extraction**.

- **Extraction: experimental — send the document schema as a required tool instead of describing it.** `extraction.forced_tool.enabled: true` declares your schema as a Bedrock tool the model must call, so a malformed-JSON reply becomes structurally impossible for the fields the schema declares; routes that cannot carry a tool schema fall back to the prompt automatically. ⚠️ **Off by default and unproven** — it constrains the shape of the reply, not the accuracy of the values in it. **Configuration → Prompt Preview** gains a **Tool Schema** tab showing the exact tool that is sent, with its tokens included in the previewed total, so the cost of turning this on is visible before you do. See [extraction and confidence](docs/extraction-and-confidence.md).

- **Extraction: advanced mode can stop sending the document schema three times.** Two of the three copies are the same JSON string — 2,600 of 6,680 schema tokens on the lending `Payslip` class — and `extraction.agentic.restate_schema_in_system_prompt: false` drops the duplicate. The benefit is context-window headroom rather than dollars, since all three copies sit inside the prompt cache. ⚠️ **It defaults to on, and is an A/B knob rather than a recommendation:** restating a schema in prose often improves adherence, so measure completeness on your own documents first — a token saving that loses list rows is a loss. **Configuration → Prompt Preview** counts the duplicated block — as a floor, since the real block the backend generates is larger — so the saving is visible before you switch it off ([#710](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/710)).

- **Classification: the model now reports how sure it is of the class, and “not scored” is distinguishable from “certain”.** Every page and section previously carried a hardcoded `confidence: 1.0`, so a clear W-2 and an ambiguous form that could have been three things looked identical. The default `classification.confidence.mode: topk` asks for ranked candidate classes with probabilities ("80 % W-2, 15 % 1099") rather than one self-rated number, and the score has its own sortable **Class conf.** column with the model's reasoning and alternatives behind it. ⚠️ **A fabricated `1.0` is no longer written anywhere** — an unscored page or section is `null`, and `1.0` is reserved for paths that genuinely assert a class. How useful the number is depends strongly on the classifier: Nova 2 Lite answered exactly 0.95 on 90 % of pages, while Haiku 4.5 produced a graded distribution. See [classification](docs/classification.md) ([#673](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/673)).

- **Classification: the per-page `document_boundary` signal is persisted** — the `start`/`continue` decision that `sectionSplitting: llm_determined` splits on is now on the page record and exposed on the API, so an unexpected section merge can be inspected after the fact rather than re-derived from Lambda logs.

- **Evaluation: the report is a React view that leads with the accuracy figures.** It was a markdown artifact in a panel, so the headline number had to be read out of prose and nothing could be sorted, filtered or linked. Extraction and classification accuracy are now separate figures, because they fail independently; a figure with no value is omitted rather than shown as 0%, and a document that could not be scored says so. The markdown report is still generated and one click away.

- **Test Studio: annotating a test set now creates a version of it explicitly, and preserves the labels it moves away from.** A version number was immutable but its *content* was not — annotation wrote straight to the live baseline — so a run stamped `TestSetVersion = 3` could not be reproduced against the labels it actually scored. **Start annotating** now opens a transition shown as `v1 → v2` and snapshots the current baselines before editing, capturing a set that arrived with ground truth as **As uploaded**; a **Test set version** picker on the run form pins a published version. See [test studio](docs/test-studio.md).

- **Test Studio: classification errors are now visible at the run level, and the annotation queue shows what each document was classified as.** A wrong class makes every field for that document unreliable, and it can be *confidently* wrong — so the document raises few alerts and sorts last in a worst-first queue. A **Classification errors** panel on the test run lists each affected document with expected vs predicted class and pages, deep-linking into the queue, and any field can be shared directly with **Copy link to field**. Shown, not scored: it is deliberately kept out of queue ordering and the review-effort estimate, whose calibration is defined on field confidence.

- **Annotation: a wrong packet split can be corrected by dragging pages between sections, without losing the annotations already on the document.** The grouping is ground truth — it is what classification is scored against — but the editor showed it read-only, so the only repair was re-running extraction and destroying the field values being protected. Saving a new grouping writes the grouping and the class and nothing else: extracted values, their `reviewed-human` provenance and edit history all survive, and re-extraction is offered per section rather than performed. Page order within a section is editable too, since it is scored in its own right; every page has a **Move to** menu so the board is usable without a pointer, and can be opened at full size to read it. Available in Test Studio and on a processed document; refused while a document is `QUEUED` or `RUNNING`.

- **Configuration Profiles: every save records a revision, so it no longer destroys the configuration it replaced.** Previously the only way to keep the previous state was creating a new named profile — and that is Admin-only, so a scoped Author could not iterate without losing work. Every save records an immutable revision you can diff, restore, label or delete, each able to carry a note saying what changed. A stack upgrade cuts a revision of `default` instead of overwriting it, which makes an upgrade's configuration changes diffable and rollback a single restore. See [configuration profiles](docs/configuration-profiles.md).

- **Configuration Profiles: pin a revision for processing, testing and labeling.** Everything ran under whatever the profile held at the time, so you could not run one test against `lending` r5 and another against r7 — the comparison revisions exist to make possible. A **Configuration revision** picker sits beside the profile picker in Test Executions, Upload Documents, Reprocess and Generate draft labels, and `--config-revision` on the CLI. Every document is pinned at queue time whether or not you chose a revision, so a save made mid-flight cannot change the configuration under an in-flight document.

- **Configuration Profiles: revisions are readable from the CLI and SDK, so an automated tuning loop can iterate inside one profile.** `config-upload` now reports the revision it created, `config-list` returns the revision counters, and revisions can be enumerated and fetched. A loop previously had to encode each attempt in a *profile name*, and every one of those is an access-control object, a document partition and a permanent table row.

- **Configuration Profiles: a “Create profile” button on the profiles table.** Making a profile from an existing one — how most profiles actually get made — was only reachable as "Save as Profile…" in the editor's Actions menu one level down, so it required knowing that "Save as" means "copy".

- **Reporting: a SQL layer — hourly and daily rollup tables over `metering`.** Every 7-day or 30-day dashboard load ran a live Athena aggregation over raw Parquet: GBs scanned, 15–90s, and frequent `503`s at the 29s API Gateway ceiling; rollups answer the same questions in KB-scale scans. A fifth table, `control_plane_hourly`, separates what the IDP infrastructure itself costs while idle from document processing, and Bedrock token cost from control-plane agents is now populated rather than always zero. The analytics chat agent can query all of them. See [reporting SQL layer](docs/reporting-sql-layer.md).

- **Monitoring: three alarms and two dashboard widgets for the SQS layer, which had none.** A document could dead-letter, or the queue could stop draining, with every existing alarm reading `OK` — both existing alarms read the state machine, and a document that never got an execution emits no metric there. `DocumentQueueDLQAlarm` and `QueueSenderDLQAlarm` fire on the first message with no threshold to tune ([#761](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/761)).

- **Models: xAI Grok 4.6 is selectable, and is the first non-Claude/non-Nova model that works for agentic extraction.** Available as `us.xai.grok-4.6` and `global.xai.grok-4.6` (the cheaper of the two); unlike the GPT-5.x family it is served on the standard Converse API, so it flows through the normal client path. ⚠️ **Three capabilities the AWS model card advertises do not work, verified live** — the Flex and Priority service tiers, `temperature`/`topP`, and document input — so no tier-suffixed IDs are offered, sampling parameters are stripped, and Grok is not offered for Discovery. Request access in Amazon Bedrock before selecting it.

- **Document List: Test Studio's documents are reachable again.** Test-run submissions moved to their own partition in 0.6.5 and the API has taken a `view` argument ever since, but no UI passed it — so those documents looked deleted. A **Production / Test Studio** switch now sits beside the search box.

- **Document List: a `Latest` option in the Load menu, now the default.** The menu topped out at 30 days and defaulted to 2 hours, so a stack quiet for longer opened on an empty table that read as a broken deployment. ⚠️ It is capped at 200 rows and says so — the counter reads `(N+)` when more remain — and the new default applies only where no preference is stored.

### Changed

- **⚠️ Extraction: turning on `x-aws-idp-multi-instance` requires migrating that class's evaluation baselines.** Evaluation compares a prediction against a baseline *of the same shape*, so a wrapped prediction against a flat baseline scores every field as missing-on-one-side and the class's accuracy collapses to ~0 **with no error anywhere**. Migrate in the same change as the flag: `python3 scripts/migrate_multi_instance_baselines.py --stack-name <stack> --apply` (dry run by default). It migrates shape, not content — the extra records for documents that really contain several were never in the baseline, so you must add them. Evaluation logs a warning naming the class when the two shapes disagree.

- **⚠️ Extraction: `extraction.agentic.validation` moved to `extraction.validation`, and validation is now ON by default.** Simple extraction runs the same validate-and-retry path, so the setting was no longer agentic-only. Stored configurations migrate automatically on read — no action required — and `config_format_version` advances to `0.7`. ⚠️ **A profile that turned validation on but never named a `fail_action` stops escalating on upgrade**, since it was inheriting the old `escalate` default; set `fail_action` explicitly if you relied on it.

- **⚠️ Evaluation: confidence-curve keys no longer depend on how many rows a list happened to have.** The index was keyed off list *length* rather than depth, so a one-row table keyed `Transactions.date` while a two-row table keyed `Transactions[0].date`, and curve points did not join across documents. Keys for single-element lists change shape (`F.x` → `F[0].x`): points stored under the old key will not join with new ones, but a list whose length varied never joined at all and now does.

- **⚠️ Reporting: `metering` is partitioned by write time, not queue time.** The existing `timestamp` and `date` columns change meaning — for metering, write time is document-completion time, since the writer runs at workflow end. Queries that assumed queue-time partitioning need adjusting.

- **⚠️ Chat companion: sub-agents now persist their conversations to `ID_HELPER_CHAT_MEMORY_TABLE`.** Four of the six in-tree agents were constructing a memory provider and never passing it in, so their sessions were not retained. Behaviour changes on deploy: those conversations now survive.

- **Configuration Profiles: “configuration version” is now “Configuration Profile”, and “version” means a snapshot of one.** One term was doing two jobs, producing the unusable phrase "versions of a config version". A **profile** is the named entity — the access-control unit, the document partition, and what you activate — and the extraction editors' "Revision History" tab is now **Edit history**, so *revision* means exactly one thing. The CLI and SDK accept `--config-profile` / `config_profile=` alongside the old `--config-version` / `config_version=`, which keep working.

- **Configuration Profiles: a feature or extension install cuts a revision of one profile instead of minting a profile per release.** Twelve releases of an extension left twelve profiles behind, and a profile is not a version: it is an access-control object an admin must add to every scoped user, a document partition, and a permanent table row. ⚠️ Existing `<featureId>-v<version>` profiles are left alone by an upgrade; they are swept on uninstall or deletable with `idp-cli config-delete` ([#697](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/697)).

- **Configuration Profiles: a scoped Author can iterate on their own use case without an admin.** Restoring and labeling revisions of a profile in the caller's scope are now Admin+Author, since a revision is content inside a profile the Author already owns. Creating a profile and deleting a revision stay Admin-only.

- **Configuration Profiles: resolving the active profile no longer scans the configuration table on every queued document.** Activation writes a pointer record read with a single `get_item`. DynamoDB bills a scan on full item size regardless of projection, so this path was getting more expensive with every profile saved.

- **Configuration validation: a Bedrock ARN whose resource looks like a model ID but is not a known model is now rejected**, instead of warning. This turns a typo in an ARN's resource-id, partition or account into a loud upload failure rather than a silent misconfiguration. ⚠️ A legitimate ARN newer than this release's model list will also be rejected; name the model directly, or upgrade.

### Fixed

- **OCR: small, faint and skewed characters are no longer silently dropped.** With `ocr.image` unset — the default for every shipped preset — pages were rendered at about 110 DPI for A4, below what Textract needs. Affects every document processed with a default configuration; no action needed beyond upgrading. ⚠️ **Re-running OCR on an existing document can now yield more text than before**, so extracted values for affected fields may change from `null` to a real value, and stored page images are larger ([#729](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/729)).

- **Classification: multi-page documents are no longer split into one section per page.** With `sectionSplitting: llm_determined` (the default), an unpaginated multi-page form or statement was fragmented into a section per page. ⚠️ **Still incomplete:** an unpaginated multi-page document is split roughly 40% of the time even with the fix, because the rules lean on pagination markers — corpora whose scans lack them are affected most. A long table whose pages reprint a running header and paginate with a bare page number can also still mis-split, with a per-class workaround documented. ⚠️ **If you have customized `classification.task_prompt`, your stored prompt wins and you will not get this fix** — re-apply it, or reset the prompt to the default ([#653](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/653), [#750](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/750)).

- **Classification: a section holding several documents no longer discards all but the first** ([#687](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/687)).

- **Classification: `sectionSplitting` is no longer ignored for single-class configurations, or when a document's name matches a class regex** — in both cases splitting was skipped entirely, so a multi-document packet arrived as one section ([#686](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/686), [#705](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/705)).

- **Classification: a failure's per-page detail now reaches the stage that reports it** ([#706](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/706)).

- **Extraction: re-uploading a document with the same filename no longer shows the previous document's extraction** ([#719](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/719)).

- **Extraction: saving a section no longer erases the processing issues recorded against it**, which left section status icons blank after an extraction write.

- **Evaluation: reports no longer contradict themselves on list-valued documents.** A parent list field read ✓ while the expanded drilldown showed red child rows, and section metrics could report 100% precision on a document with most of a list wrong ([#625](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/625)).

- **Evaluation: a class with a single top-level group is no longer scored with no confidence at all**, which the report showed as "nothing to measure" rather than as a gap. ⚠️ Affected classes reported **no** calibration before this fix and report real numbers now, so **calibration metrics on earlier runs are not comparable with current ones** for those classes ([#713](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/713)).

- **Evaluation: a list row containing a nested group or an inner list is no longer reported as unscored** however well the model scored it — the check looked exactly one level down, so a nested group was mistaken for a confidence leaf and the whole row was written off.

- **Rule validation: the Z3 engine can now address a list element, and a wrong path fails instead of going quiet.** `LineItems[2].amount` was unreachable, and because a miss resolves to "optional parameter absent" a rule whose path had become wrong silently stopped validating anything.

- **Monitoring: the alarm for “workflow executions are failing” had never once fired, and no alarm notification was ever delivered on a default deployment.** The primary failure alert was defined against a metric name Step Functions does not emit, and separately, every alarm in the stack was console-only with no SNS action — so a stack could be failing every document with no notification anywhere. `WorkflowTrackerDLQAlarm` now also notifies on recovery, since DLQ depth does not decay on its own. ⚠️ **If you upgrade a stack that has been alarming silently, expect notifications to start arriving immediately** — that is the fix working. With `ErrorThreshold` defaulting to `1`, a stack with ongoing failures produces one alert per 5-minute window containing a failure ([#746](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/746)).

- **Discovery: re-running on a class that already existed erased every class-level setting an author had put on it.** The write path replaced the class wholesale while the discovery model emits only the class id and properties, so per-class extraction model, escalation model, prompts, confidence threshold, regexes, page types, exclusions and few-shot examples were all silently dropped — visible only in the next document processed ([#764](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/764)).

- **BDA: a job whose completion callback never arrives no longer hangs the workflow forever.** The `waitForTaskToken` step had no timeout, so a lost callback blocked the execution indefinitely and held a concurrency slot ([#755](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/755)).

- **Models: a retired Bedrock model is no longer offered in the picklists.** Selecting it produced a `ResourceNotFoundException` at runtime. ⚠️ **A stored configuration that still pins this model keeps loading, and `idp-cli config-validate` now rejects it** — model IDs are plain strings, not a closed enum, so nothing revalidates a stored configuration for you; change it before your next run ([#708](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/708)).

- **Models: a model named by an inference-profile ARN is recognized by the sampling-parameter capability gate**, so a configuration using the ARN form the docs recommend no longer sends parameters the model rejects. Service-tier suffixes on IDs with no version segment now parse, and Chat-with-Document no longer sends `temperature` to models that reject it.

- **Test Studio: a draft-labeling run no longer reports accuracy it does not have.** Such a run has no baseline by construction, so it showed `Page Level Accuracy 0.000` and empty score tables under an alert explaining there were no metrics, and could sit badged `EVALUATING` indefinitely while re-aggregating on every view. Clearing a set's draft labels also left it permanently claiming to have them with an estimated accuracy for labels that no longer existed, a set that arrived with ground truth was labelled `Unlabeled` in the editor while two other statements on the same screen said otherwise, and the labeling dialog described a 100-document set as 50 while labeling all 100.

- **Test Studio: uploading a test set from a zip works in the layout the wizard documents, and keeps the name you typed.** A zip built exactly as the **Required zip structure** diagram shows produced 0 documents, and the set took the archive's filename instead of the name collected on the form.

- **Test Studio: files dropped directly into an existing test-set folder in S3 no longer stay invisible** — already-registered folders were short-circuited, so a manual add updated neither the file count nor the input↔baseline pairing.

- **Test Studio: test sets can be listed in private-network deployments.** With `S3PresignedUrlViaVpcEndpoint=true`, the resolvers aimed their own S3 calls at the interface endpoint hostname and `getTestSets` returned a bodiless `504`. A data operation that exceeds the gateway's integration limit now also returns an error you can read, naming the call.

- **Test Studio: “Estimated cost” no longer under-reports a run whose documents finish more than a day after it started.**

- **Test Studio: the class dropdown in the test-set editor offers the classes the labels were produced with**, not the built-in lending-package preset ([#662](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/662)).

- **Annotation: correcting a misclassified document's class now actually changes it.** It reported success while silently discarding the correction — a flow that had shipped broken for several versions and was found in the field. An Admin could also change a class in the document view but not in the annotate view and was told it was a permissions problem, and a correction that did run to completion reported failure to the UI ([#674](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/674)).

- **Annotation: correcting a document in a set that arrived with its own ground truth now counts as reviewed.** The queue read "0 of 73 documents reviewed" however many were fixed, above a toast saying the document had been marked reviewed.

- **Annotation: an edit no longer keeps the model's confidence attached to your typing, and one edited table row no longer claims authorship of its whole column.** The edited-path key dropped array indices, so correcting `LineItems[3].Rate` marked every row's Rate cell as human-edited. Losing an edit also took one click.

- **Annotation: the editor is usable with a screen reader.** Every field value was a text input nested inside an element claiming `role="button"`, which ARIA forbids — so each field announced twice and the click-to-locate capability was unreachable by keyboard.

- **Annotation: a reviewed document can be reached again without finishing every other one.** Reviewing dropped it out of the queue and the only route back was an action on the "Queue complete" alert, which by definition appears once all remaining work is done.

- **Annotation: two dead ends on an annotator account are gone** — a set badged "Needs labeling first" above a link to open its queue, when generating those labels is Admin/Author only.

- **Configuration Profiles: saving a new profile onto a name that already exists no longer silently overwrites it.**

- **CLI: `config-list` no longer fails outright if any one profile predates the `IsActive` attribute** — one such profile took down the whole listing. ⚠️ **Scoped users will see fewer documents after this upgrade**: any that disappear were never stamped with a profile, and reprocessing them under a profile in that user's scope restores visibility. Admins and unscoped users are unaffected.

- **Analytics: the agent's classification-count example no longer renders `FROM document_sections_<class 'type'>`** — an f-string interpolated a placeholder that was meant to be literal.

- **Web UI: a section skipped because its class is excluded from processing now shows the “Skipped” badge that explains why its pages have no data** ([#704](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/704)).

- **Deployment: a custom domain now serves the Web UI's data operations, not just its pages.** With `CustomDomainUrl` set under `WebUIHosting=APIGateway`, the bundle still called the raw `execute-api` hostname, which a browser reaching the app through the vanity domain often cannot resolve.

- **Processing: a concurrency-drift misconfiguration can no longer wedge document intake permanently.** Setting `RECONCILE_SAMPLE_MAX_AGE_SECONDS` below `RECONCILE_GRACE_SECONDS` by hand left an empty window in which no drift sample could mature, so a leaked concurrency counter was never corrected and the stack stopped admitting documents once it reached `MaxConcurrentWorkflows`. The defaults were never exposed; max age is now clamped to at least 2× grace with a warning naming both values.

- **Extensions: updating an already-installed ConfBench Test Set extension no longer fails on Lambda's package-size limit.** A stack installed before 2026-08-13 could not be updated to any later version.

- **Security: five HIGH dependency advisories are resolved on shipped code paths** — `mistune` (RecursionError denial of service, `GHSA-6m44-fpc8-c3rq`) bumped to 3.3.4, and four host-confusion / SSRF advisories in `fast-uri` reached through the web UI's `ajv`, pinned forward on the 3.x line.

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.6.7.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.6.7.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.6.7.yaml`

## [0.6.6]

### Added

- **Bulk document download from the document list.** The list's **Download** menu now offers **All data** / **Predictions** / **Baselines** for every selected row, producing one archive with a folder per document plus a `manifest.json` — previously the only way to collect a set of extractions was to open each document and download it individually ([#679](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/679)). Selections above 25 documents warn about size and time, since the archive is built in the browser. See [docs/web-ui.md](docs/web-ui.md#bulk-download-from-the-document-list).

### Changed

- 🔒 **Pillow → 12.3.0, closing 18 advisories (11 HIGH) on the untrusted-document path.** Attacker-supplied images and PDFs reach Pillow directly through OCR, image conversion and discovery, so the heap out-of-bounds writes and decompression-bomb DoS in those advisories were reachable rather than theoretical — including in the PII Anonymizer extension, which an arm64 wheel constraint had been holding below the fix. Also `nanoid` → 3.3.18 and `js-yaml` → 4.3.1 in the UI and docs-site build trees (GHSA-2v37-7h3g-55p8, GHSA-5p4m-2wfm-xmqj, both HIGH); both now report zero HIGH audit findings.

- 🔒 **The VPC endpoint security group no longer allows all outbound traffic.** `scripts/vpc-endpoints.yaml` omitted `SecurityGroupEgress`, which CloudFormation renders as allow-all-outbound; egress is now pinned to HTTPS inside the VPC. Interface-endpoint ENIs only answer inbound 443 and security groups are stateful, so nothing that worked before stops working.

### Fixed

- **A stack could stop accepting documents permanently, with every metric reading idle rather than broken.** The workflow-concurrency counter drifted upward whenever a decrement was missed and nothing ever put it back, so once it reached `MaxConcurrentWorkflows` the queue stopped draining forever — observed live with 2,532 messages in flight and nothing processed for over two and a half hours. The counter now self-reconciles when it is actually blocking work, two paths that could lose a slot are closed, and new drift/active metrics, a dashboard widget and two alarms make the condition visible instead of silent. ⚠️ This grants two new permissions on stack update — `states:ListExecutions` on the stack's own state machine and scoped `cloudwatch:PutMetricData` — so a deployment using a restricted CloudFormation service role may need them added ([monitoring.md](docs/monitoring.md), [aws-services-and-roles.md](docs/aws-services-and-roles.md)).

- **A failed evaluation destroyed the document it had just finished processing, after holding a workflow slot for five hours.** An evaluation timeout is deterministic, but it was retried 8 times at 2.5× backoff — ~5.2 hours per document — and the eventual failure discarded that document's completed OCR, classification, extraction, assessment and summarization. Timeouts now get one attempt, and any evaluation failure records an honest `EvaluationStatus` (the new `TIMED_OUT`, or `FAILED`) and lets the document complete; previously a timeout left the status stuck at `RUNNING` forever.

- **Evaluating a list field with the `LLM` method could stall an entire deployment.** List matching invoked the comparator once per cell of an N×N matrix, so a 54-line-item invoice needed ~3,000 sequential Bedrock calls (~45 min) — unfinishable inside the evaluation Lambda, which triggered the retry ladder above and wedged 29 documents with 2,532 queued behind them. `LLM` on a list-item field is now downgraded to that field's deterministic default (Levenshtein / Numeric / Exact) with a warning, `x-aws-idp-evaluation-allow-llm-in-list: true` opts a small bounded list back in, and identical values no longer cost a model call at all. ⚠️ The judge was also being called with no field name or description, so **`LLM`-method scores from earlier releases are not directly comparable with current ones.**

- **A list that came back missing, or far shorter than the schema demands, was reported as a clean success.** Extraction returns complete lists up to ~800 rows, then recall falls to **0.199 at 1,200 and 0.009 at 3,200** — and the loss is near-undetectable, because scalar accuracy stays perfect and a truncated run is *cheaper* ($1.78 → $1.04). Absent, null and short lists now raise warnings in both extraction modes, surfacing as processing issues on the section, so they reach the document list's **Processing Issues** column and the **Processing Report** tab. Set `minItems` on list fields you care about for the unambiguous signal — see [docs/extraction-and-confidence.md](docs/extraction-and-confidence.md).

- **An agentic extraction that declined the table tool could return a whole table as `null`, and the report called it clean.** Observed at Sonnet 5 on a ~103-row table: one OCR-corrupted column led the agent to skip the parser and return no rows, leaving the section `COMPLETED` with scalar accuracy 1.000 and a `✓ All schema constraints satisfied` line printed above the warning that the list was empty. Visible rows must now be extracted regardless (an unreadable column yields a null *cell*, not a dropped row), an all-empty result on a substantial table triggers a correction round even where schema validation is off, and the Processing Report reports the loss instead of contradicting it.

- **Group fields in integrated (1S-TopK) confidence mode were unusable, and long lists were dropped.** Every group field of every document processed in this mode held a raw candidate object (`Address.City` = `{"G1": "Anytown", …}`) instead of its value and carried no confidence, so those fields were also invisible to threshold alerts and HITL. Values are also no longer shortened (the prompt asked for the shortest guess and the model obliged), and dropping the four-guesses-per-list-cell request stops long lists truncating: a 100-row list that returned 10 rows now returns 100. ⚠️ The single-response limit is fundamental, so **`separate` remains the recommendation for list-bearing schemas** ([extraction-and-confidence.md](docs/extraction-and-confidence.md)).

- **Groups and arrays authored in the UI schema editor read as untyped in four more places ([#678](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/678)).** A `$ref` group's children were dropped from the classification prompt; a `$ref` array was scored as a scalar, so its per-row confidences collapsed to one unusable default and the escalation ladder wasted a round on it; per-sub-field confidence thresholds were ignored in integrated mode (the same breach raised 2 alerts in `separate` and 0 in `integrated`); and the Web UI prompt preview disagreed with the prompt that actually ran. All four now resolve `$ref`s the same way.

- **Every GovCloud deployment rolled back on a BDA project that region cannot accept — even Textract-only stacks ([#676](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/676)).** The BDA-as-OCR project was created unconditionally with no parameter to disable it, so the stack failed with `ValidationException: Sync project does not support video/audio/document modality…` and rolled back ~30 resources regardless of deploy mode or configured `ocr.backend`. It is now created only in the commercial partition, and a region that refuses the project shape degrades gracefully instead of failing the stack. ⚠️ The `bda` OCR backend is therefore **not available in GovCloud**; use `ocr.backend: textract`, which is the built-in default.

- **`--govcloud` deployments failed on a Lambda layer that cannot exist outside the commercial partition ([#677](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/677)).** The chat streaming handler layers in the AWS Lambda Web Adapter, published only by a commercial-partition AWS account, so the deploy died on a `lambda:GetLayerVersion` 403 that no identity policy could fix — and the documented `LambdaWebAdapterLayerArn` override had no GovCloud publication to point at. The transform now removes that handler and everything referencing it. Chat is unaffected: the polling transport the GovCloud UI uses was never served by the streaming function.

- **Bedrock Data Automation and Knowledge Base queries failed in GovCloud on invalid ARNs ([#527](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/527)).** Runtime Python built ARNs with a hardcoded `arn:aws:` prefix, so every BDA invoke returned `The provided ARN is invalid` in `aws-us-gov`; the same bug broke Knowledge Base queries and blueprint optimization, silently skipped the CLI's permissions-boundary teardown (leaking an IAM policy), and left two IAM/bucket policies matching nothing while appearing to apply. The partition is now derived at runtime everywhere, which also fixes a wrong `ap`-for-`apac` prefix in Asia Pacific regions. ⚠️ The `bda` OCR backend and BDA mode remain unavailable in GovCloud for the separate reason in [#676](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/676).

- **Every `--headless` deployment had been failing template validation since 2026-07-16, before creating a single resource.** The headless transform removes the `AdminEmail` parameter but left behind a condition referencing it, and CloudFormation rejects the whole template up front with `Template format error: Unresolved dependencies [AdminEmail]`. The condition is now stripped; it was already inert, so headless deploys are unblocked with no behavioural change.

- **A valid sign-in could land on a permanently blank page, with no error and no way out but a reload nobody suggested.** Duplicate credential requests raced on start-up, Cognito rejected one of them as a duplicate, the failure was swallowed, and the app — gated entirely on those credentials — rendered nothing for up to 15 minutes, stranding any background job the page was polling. Credential fetches are now shared across the app and retried on a short backoff, and a page that still cannot get them shows an actionable **Retry / Reload** error instead of an empty one. This also fixes a leak that started a new 15-minute session-refresh timer on every auth state change.

- **Downloading a document's Baselines produced nothing but 403s, and "All data" silently dropped its baseline half.** The browser signed baseline URLs with credentials that deliberately have no read access to the evaluation baseline bucket, so every baseline landed in the archive's error list as `HTTP 403 Forbidden`. Baseline assets are now signed by the backend, which repairs the per-document download shipped in 0.6.5 as well as the new bulk export — on already-deployed stacks, with no stack update required.

- 🔒 **The multi-document discovery pipeline no longer passes a pickle between steps.** One step wrote `cluster_data.pkl` and the next called `pickle.loads()` on it — an arbitrary-code-execution sink on load — for a payload that was plain numbers throughout; the artifact is now `cluster_data.json`. ⚠️ A discovery job already in flight across the upgrade fails and must be re-run, since the two steps disagree on the artifact format.

- **Seven invalid CSP `connect-src` sources logged a console error on every page load.** Each placed a wildcard in the middle of a host, which CSP does not permit, so Chrome discarded them — they were also redundant with the `*.amazonaws.com` entries that already match those hosts, which is why the app worked throughout. Removed, so the policy states what it actually enforces.

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.6.6.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.6.6.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.6.6.yaml`

## [0.6.5]

### Added

- **Host support for paid AWS Marketplace extensions, starting with Auto Optimizer (Beta).** The feature platform could describe a Marketplace extension but not serve one. Four things are now in place. **(1) Per-region publishing (catalog schema 1.1)** — `sam package` bakes an absolute, region-specific `CodeUri` into a published template and a Lambda's code bucket must be in its own region, so `config_library/extensions-marketplace.yaml` gains an explicit `regions` map plus `productId`, and the launch resolver looks the caller's region up and fails closed with "not available in `<region>`" rather than deriving a bucket name that could resolve to a bucket someone else owns. **(2) Region availability in the UI** — the catalog browser and feature page show where an extension *is* available, computed with the same helper the launcher uses so badge and launcher cannot disagree. **(3) A production entitlement path** (`marketplace-live`, the new default when no simulator endpoint is set) built on the **buyer-side** Marketplace Agreement API (`SearchAgreements`, `PartyType=Acceptor`, `Status=ACTIVE`). New [After Subscribing on AWS Marketplace](docs/marketplace-subscription-next-steps.md) page for a listing's post-subscribe URL. Docs across both tiers: [feature-platform.md](docs/feature-platform.md), the [developer guide](docs/feature-platform-developer-guide.md), [auto-optimizer.md](docs/extensions/auto-optimizer.md).

- **"Update available" badges now read `latest.json` at runtime, so an extension release no longer requires an accelerator release.** `latestVersion` was baked into `catalog.json` at *host*-publish time, so a new extension version stayed invisible until the whole accelerator was re-released **and** the customer took that release. The publisher has always rewritten `<base>/latest.json` on every release; the host simply stopped reading it. `listInstalledFeatures` now resolves the live value with the catalog as fallback — **fail-soft** (unreachable object, bad JSON, blocked egress or an unpublished region falls back to the catalog and then to no badge, never an error), **cached** (memoized per bucket/key for `LATEST_JSON_TTL_SECONDS`, default 300s, with a shorter negative TTL and a capped fan-out so wall time is one S3 round trip), and **anonymous first** (published artifacts are public-read, so the unsigned GET needs no new IAM grant and enabling it cannot regress an existing deployment; a refused public read retries signed). `LATEST_JSON_LOOKUP=false` reverts to catalog-only.

- **A ready-made Seller Entitlement Service for paid extensions** (`feature-platform/seller-entitlement-service/`). A Feature Platform extension deploys into the **buyer's** account and the buyer owns its Lambda, environment and code — so software running there cannot enforce its own licence, and the host's entitlement check is advisory by construction. Enforcement requires the *seller* to hold something the buyer needs at runtime. This is a self-contained SAM stack a seller deploys once in their own account: an extension POSTs `{"productId": "prod-…"}` and receives a short-lived, account-bound token signed by a KMS **asymmetric** key (so an extension can verify with the public key without being able to mint), carrying a `kid` so the key can be rotated. Authentication is API Gateway **`AWS_IAM`** with a resource policy admitting any AWS principal, and the buyer account is read **only** from `requestContext.identity`, never the request body — so the seller needs no advance knowledge of buyers and buyers need no seller-issued credentials. Deliberate asymmetry with the host: this service **fails closed**, while the extension is required to hold a grace period on the last-known-good token so a seller-side outage never locks out a paying customer. 

- **Test sets are now versioned, provenance-tracked benchmark objects, and can be built from unlabeled documents.** A test set previously had to arrive with complete ground truth and kept no record of where its documents came from or whether a human had ever checked the labels. It now has a mutable working draft plus immutable published versions (`v1`, `v2`, …) and an *active reference* that scoring follows, and each test run pins the test set version alongside the config version it already captured — so a metric that moved because the *ground truth* changed is distinguishable from one that moved because the *configuration* did. **Draft labeling** runs the active configuration over documents with no `baseline/` folder, through the ordinary OCR → classification → extraction → assessment pipeline, to produce ground-truth candidates with per-field confidence; every label is tagged `draft-machine`, `reviewed-human`, `uploaded` or `synthetic`, and re-labeling replaces *only* machine drafts, so reviewed and hand-uploaded ground truth survives a re-run with a newer config. Documents can be removed from a set, sets record whether their documents were uploaded / generated / mixed, and Admin/Author can clear draft labels or reset all labels for re-labeling under a corrected config. **Backward compatible:** pre-existing test sets read as never-published drafts, their runs pin no version, and no backfill is needed. Known limitation: a published version snapshots metadata rather than the label *bytes* — all versions share one `baseline/` prefix, so editing the draft changes what a published version resolves to. See [docs/test-studio.md](docs/test-studio.md#versioning-test-sets).

- 🔒 **Team annotation: a scoped, collaborative, worst-first queue for ground-truth review, and a new `Annotator` role.** Several people can now review one test set in parallel from a shared queue ordered by confidence alerts, with claim-to-lock exclusivity (a conditional write, so two simultaneous claims cannot both win), shared set-level progress, and the *existing* ground-truth editor and audit trail rather than a parallel system. An annotator workspace (`/test-studio/sets/<id>/annotate`) puts the queue rail beside that editor and saves through `completeSectionReview`, which is what engages the `reviewed-human` tag, the revision history, and the confidence-curve learning the estimator depends on. Annotators can also repair a **misclassified** document: the class is a dropdown of the classes the document's own config version defines, and **Change class & re-extract** re-runs that one document so the corrected labels reach the baseline. The new role is least-privilege and scoped by `allowedTestSets` — an annotator can read and annotate only their assigned set(s), cannot list other sets, run configurations, publish versions or review production documents, and scope is re-verified server-side on every operation from one shared module, **failing closed** in every ambiguous state. Also here: one **Create test set** wizard replaces four near-identical modals reached from two dropdowns, and the Test Sets header drops from seven competing controls to three. See [docs/test-studio.md](docs/test-studio.md#team-annotation-the-scoped-queue) and [docs/rbac.md](docs/rbac.md#the-annotator-role).

- 🔒 **Review-effort estimator: a measured answer to "how many documents must I review?"** Test Studio can now estimate the review depth needed to reach a target accuracy from a per-set measured **confidence→accuracy curve**, returning documents-to-review, the implied confidence cutoff, residual error, effort and an error-burndown curve (`estimateReviewEffort`, Admin/Author) — fronted by a **Set up team annotation** modal that prices three strategies (review the lowest-confidence documents / review everything / accept machine labels) *before* an owner commits a team. The curve improves from a cross-set prior, each reviewer's verdicts, and scoring runs, keyed by `(test set, config version)` and stored as bin counts so concurrent reviewers accumulate instead of overwriting. Every answer states how much to trust itself (`prior` / `partially-measured` / `measured` / `unreliable`), because a number from a generic prior otherwise looks identical to a measured one. Three failure modes are detected rather than assumed away — an **overconfident** model, **degenerate** confidence, and confidence that cannot *rank* correctness at all (measured on a 100-document set: ECE 0.032 across 7 bins, passing calibration, with **AUROC 0.480** — worse than chance, all 77 errors in the top bin, so worst-first review reached none of them) — each of which recommends reviewing everything and says why. The standing mitigation for the first is a power-sized audit sample of the high-confidence zone, whose cost is included in the reported effort rather than excluded to flatter the headline. See [docs/test-studio.md](docs/test-studio.md#how-much-review-is-enough) and the [module README](lib/idp_common_pkg/idp_common/evaluation/README.md).

- **Z3 Dual-Engine Rule Validation** — Rules can now be individually routed to either the LLM (semantic) or Z3 (formal/deterministic) validation engine via the `x-aws-idp-validation-engine` field on each rule property in `policy_classes`. The Z3 engine uses pre-translated SMT-LIB constraints (stored inline as `x-aws-idp-rule-json`) to validate extracted parameter values deterministically. A "Generate RuleJSON" button in the Config Editor translates natural-language rules to constraints via Bedrock. Strict mode enforced: Z3 configuration errors surface as "Information Not Found" rather than silently falling back to LLM. A new "Validation Engine" dropdown in the Schema Builder UI lets users select the engine per rule. Requires `z3-solver` (optional dependency, loaded lazily only when Z3 rules are encountered). See [`docs/rule-validation-z3.md`](docs/rule-validation-z3.md).

- **A class that disagrees with ground truth is now flagged where the class is shown ([#627](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/627), [#630](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/630)).** Arriving at a low-scoring document, there was no way to tell whether the score was low because a field was extracted wrongly or because the page was classified as the wrong document type in the first place — the per-page comparison was already in `evaluation/results.json` but only section-level tables rendered it. The **Document Sections** and **Document Pages** tables (including Edit mode, next to the class dropdown) now show a **Class mismatch** alert beside a class that disagrees with ground truth, with a hover giving the expected class, the class this run assigned, and the pages involved; the Visual Editor's **Show Evaluation** toggle compares the section's `document_class` above the field list; and all three explain that a misclassified page was extracted against the **wrong schema**, so its extraction score is a symptom rather than the cause. A section's verdict is derived from *its pages*, so a section that merged two ground-truth documents reports both classes it spans instead of an unhelpful "no match". Deliberately silent where it should be: a matching class gets no annotation, a page ground truth says nothing about is left unannotated, and a document with no evaluation renders exactly as before with no added screen space and no request made. See [docs/evaluation.md](docs/evaluation.md#seeing-which-pages-were-misclassified-web-ui).

- **Per-field accuracy now reports its sample size and margin of error.** Test Results showed each field's accuracy as a bare point estimate, so 100% measured on 3 observations and 100% measured on 300 rendered identically. That matters more per field than overall: a field appearing once per document gains one observation per document, so a badly-broken field can sit inside a healthy-looking overall score. Two new columns — **Observations** and **95% margin** — with the interval in the tooltip and fields whose margin exceeds 10 points rendered subdued. Derived from confusion-matrix counts already stored, so there is no new measurement, no schema change and no migration; runs aggregated before this release show both columns too. Uses a Wilson score interval rather than the normal approximation, which at 20 observations and 90% accuracy reports an impossible 103% upper bound in exactly the low-evidence case the columns exist to surface. Documented limits: the interval covers sampling uncertainty only — not errors in the ground truth, and not repeated observations within one document (table-row fields are not independent, so their margin reads tighter than it should). See [docs/test-studio.md](docs/test-studio.md#field-level-metrics).

- **Textract `SIGNATURES` is on by default, and signature detections are finally usable end-to-end ([#634](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/634)).** A `SIGNATURE` block is a confidence plus a bounding box with **no text**, so every LINE-oriented consumer skipped it and the only trace left was the linearizer's inline `[SIGNATURE]` token — unpositioned and unqualified, making a real signature byte-identical to a 10%-confidence smudge. Detections are now carried in three places: `pageData.json` gains a `signatures` array, `textConfidence.json` and the parsed page text gain an `OCR signature detections` block giving each detection's confidence **band** and its position in the left/right + upper/lower language field descriptions use, and the Web UI page viewer lists them under **Signature detections** with a clickable box. `ocr.features` now defaults to `TABLES` + `LAYOUT` + `SIGNATURES`, which adds **no Textract charge** in that combination (per the [pricing page](https://aws.amazon.com/textract/pricing/); used *alone*, `SIGNATURES` bills ~$0.0035/page, so the defaults must keep a paying feature alongside it). A detection is **not** proof of a signature, and `docs/configuration.md` now documents the recipe that makes a signed/unsigned boolean deterministic: a field description that turns the detection confidence into an explicit decision rule ("answer false unless a detected region with confidence >= 50 falls in this box"), plus few-shot examples. Enabling this adds a few prompt tokens only on pages that have detections; corpora with no signature fields should drop `SIGNATURES` from `ocr.features`.

- **Few-shot examples defined on a class are now actually used by extraction and classification, and both key spellings are read ([#634](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/634)).** Defining `x-aws-idp-examples` used to be necessary but not sufficient, in two silent ways: **no default prompt contained `{FEW_SHOT_EXAMPLES}`**, so examples reached the model only if you also hand-edited the prompt, and the example reader **only understood the legacy camelCase keys** (`classPrompt` / `attributesPrompt` / `imagePath`), so a config authored from the docs was skipped as an "example with empty attributesPrompt". All three extraction prompt variants and the classification prompt now carry the placeholder (before `<<CACHEPOINT>>`, since examples are static per class), rendering empty for classes with no examples, and both key spellings are accepted. The `x-aws-idp-*` spelling is now canonical end-to-end — config migration converts entry fields, the UI's schema editor writes canonical, and the shipped preset, notebooks and docs are converted — but readers keep accepting camelCase indefinitely, so **no existing config needs to change**. An unreadable `imagePath` now degrades to a text-only example with a warning instead of failing the section. ⚠️ Note this changes prompt content (and therefore cost and behavior) for any class that has examples; a config that overrides a shipped `task_prompt` with its own copy still needs to add the placeholder itself. See [docs/few-shot-examples.md](docs/few-shot-examples.md).

- **~400 previously-ungated test suites now run in CI.** No workflow referenced `feature-platform`, `idp_feature_sdk`, `idp_sdk` or the seller service — the `developer-tests` job ran only `lib/idp_common_pkg` plus the UI, and the `make test-packages-cicd` target that covers them existed but was never called. It is now called, and gains the seller-service suite. Switching it on required a new `make install-first-party`, which installs all five first-party packages in a **single** pip pass — deliberately single, because they depend on each other by bare name and those names are squatted on public PyPI, so a split install resolves a sibling from PyPI — followed by `scripts/check_first_party_deps.py`, which fails if any did. Verified in a throwaway worktree replicating CI: 1,055 tests across 11 suites pass. Two latent problems surfaced: `src/lambda/chat_stream_processor/tests/` had no `conftest.py`, so a test that builds a boto3 client at import scope passed only where a region happened to be configured; and the ConfBench extension's 93 tests were in neither `RUN_ROOTS` nor `QUARANTINE`, which **hard-failed `run_all_tests.py`'s own registration guard** — the guard that exists to stop new tests being silently skipped was blocking the whole `make test` gate. Both fixed; the gate now covers 46 roots.

### Changed

- **BREAKING (operators): entitlement authority is now declared PER EXTENSION (`licenseMode`), not chosen once for the whole stack.** One stack can finally host a listed extension verified against **real AWS Marketplace** alongside **in-development** extensions checked against the simulator and **OSS** extensions checked against nothing. Previously the authority was stack-wide, so pointing a stack at a simulator pointed it there for *everything* — including a live listed product, where the host then showed a simulator-backed "Subscription active" that the extension itself correctly contradicted. `licenseMode` (`none` | `simulated` | `marketplace-live`) is declared in two places on purpose: in `config_library/extensions-marketplace.yaml`, governing the **host's** check, and in the extension's own manifest or template, governing the **extension's** — the extension's value is propagated through `registerFeature` at install, the host prefers it, and a disagreement is reported as `licenseModeMismatch` instead of showing a subscription the extension will ignore. The prerequisite was dropping env-var endpoint overrides: `AWS_ENDPOINT_URL_MARKETPLACE_*` are per-*function*, so while boto3 read the endpoint from the environment a single resolver could only ever be aimed at one authority — that mechanism was what *forced* stack-wide behaviour. The resolver now passes `endpoint_url=` explicitly per request and anchors the reported source to the endpoint each call actually used, so **no combination of parameters can forge `marketplace-live`**; a simulator-backed check reports `simulated` however the mode is set, logs loudly at cold start, and fires the `UnverifiedEntitlementGrant` metric. **`FeaturePlatformSubscriptionMode` is reduced to a kill switch** (`auto` = check nothing on this stack); its other values no longer select the authority and are read only as step 3 of the migration chain, scheduled for removal one release after the next. **Migration:** resolution is install-row → catalog entry → legacy stack setting → `marketplace-live` for a marketplace entry / `none` for OSS, so an existing stack that never re-installs anything keeps working. A missing `licenseMode` in `extensions-marketplace.yaml` defaults to `marketplace-live` — deliberately the *opposite* of the extension-side default (`none`), because the host must never over-claim verification while the extension must never lock a paying customer out. **For extension authors:** `FeatureContext` gains no fields, so no code change is required; set `marketplace.licenseMode` in your manifest to have the host agree with your own gate. See [feature-platform.md](docs/feature-platform.md) and the [developer guide](docs/feature-platform-developer-guide.md).

- **BREAKING (extension authors): `entitlementSource` loses two values and gains one — `marketplace` and `simulator` collapse into `simulated`.** Both named the *same code path*: the seller-side `GetEntitlements` API, which returns HTTP 200 with an empty list from a buyer account and therefore cannot verify anything against real AWS. Both were already treated as unverified and already shared one explanation string, so the distinction was one no extension could act on — while making `marketplace` (the weakest source) read as more authoritative than `marketplace-live` (the only verified one). The union is now `marketplace-live | simulated | advisory | auto | oss | none`, and `marketplace-live` now means strictly "real AWS answered". An **OSS extension reports `oss` in every deployment mode** — the `auto` branch used to be evaluated first, so `oss` was not a dependable signal for "this is not a paid extension", the one thing it exists to say. **Upgrade:** if your extension matches `'simulator'` or `'marketplace'` literally, match `'simulated'` instead; extensions that only read `uiAccessAllowed` / `entitlementVerified` need no change. `SubscriptionMode` keeps all four values, so **no existing stack breaks on update**, and `marketplace-live` was deliberately *not* renamed to `marketplace`, which would silently invert the meaning of a value extensions may already match on. `sample-feature` and `sample-health-insurance-review` → 0.2.1.

- **BREAKING (extension authors): `FeatureContext.subscriptionActive` is renamed `uiAccessAllowed`.** The old name invited exactly the wrong reading — it is a boolean computed by the host and handed to code running in the end user's browser, inside the customer's own AWS account, and it reads `true` whenever subscription checks are off (`auto`), simulated, or unreachable (`advisory`). Extensions were being taught to treat it as a licence check; the reference samples gated on it and none did any server-side verification. The new name says what it is: presentation, not authorization. `entitlementSource` and `entitlementVerified` were added alongside for the honest signal. **Upgrade:** rename the field where your extension destructures it. All six bundled extensions are updated and version-bumped (`pii-anonymizer`, `sample-feature`, `sample-health-insurance-review` → 0.2.0; `confbench-testset` → 0.1.2; `idp-data-generator` → 0.1.3) so the "Update available" badge prompts admins to pick up the change. An extension installed *before* this release keeps working until updated, but renders inert (its buttons stay disabled) because the prop it reads is now absent. Done as a clean rename rather than a deprecated alias because no paid Marketplace extension has shipped to customers yet, so this is the last cheap moment to fix the name.

- **The Document List now shows only documents a user submitted; Test Studio runs have their own view.** Test Studio deliberately submits through the production pipeline so confidence and cost semantics match real runs, but that made its documents indistinguishable from customer uploads — on a dev stack, 248 of 249 rows in the Document List were test artifacts. The copier now writes submission-source and test-set-id as S3 object metadata, `from_s3_event` reads them off the HEAD it already performs, and the tracking write turns them into an `ItemType` (`document` vs `test-document`) — the `TypeDateIndex` hash key, so the two views are separate index partitions rather than a filter applied after the page limit. ⚠️ **Upgrade note:** documents processed before this release carry no provenance metadata and stay in the Document List until the `ItemType` backfill state machine runs — triggered automatically by a CloudFormation custom resource on stack update, idempotent, and resuming from a continuation token if it hits the Lambda timeout. It retypes a legacy row only when the key prefix has the copier's `<testSetId>-<YYYYMMDD>-<HHMMSS>/` shape **and** a matching test-run record still exists, since the shape alone is not exclusive to test runs and retyping a real upload would remove it from the Document List. Consequence: legacy test documents whose run records have aged out by `DataRetentionInDays` remain in the Document List, where they can be deleted individually.

- **Evaluation: Stickler integration cleanup and package reorganization.** End-to-end overhaul of the evaluation package's use of `stickler-eval==0.5.0`, fixing several silent correctness bugs: NUMERIC_EXACT tolerance and Hungarian `match_threshold` never actually reached Stickler (the tolerance gate was a no-op and `match_threshold` was written to the array property instead of the items schema), and per-document FAR/FDR diverged from the run-level dashboard because the per-doc side re-derived them from IDP's own counts — both sides now derive FAR from Stickler's `fa` and FDR from `fd` rather than the combined `fp`, so one formula answers everywhere. Stickler is now the single scoring source of truth: verdicts, counts and derived metrics all come from `confusion_matrix` directly. Newly surfaced Stickler capabilities: `DateComparator`, graded packet metrics (`final_score`, `clustering_score`, `v_measure`, `rand_index`, `avg_ordering_score`), a confidence accumulator with index-collapsing (`LineItems[N].Rate → LineItems.Rate`), weight-aware document-level rollup, and `clip-under-threshold` / `aggregate` per-field pass-throughs; `EvaluationMethod` gains `LEVENSHTEIN`. Structurally, a new `stickler_backend/` subpackage is the only code path allowed to `import stickler`, `contract.py` formalizes the cross-Lambda `results.json` payload with a `STICKLER_RESULT_VERSION` stamp, old top-level modules remain as re-export shims, and ~1,700 lines that duplicated Stickler functionality are deleted (the local `DocSplitClassificationMetrics` fork, `metrics.py`, the legacy fields-config mapper, `ConfidenceMetricsCalculator`), dropping the `munkres` dep and relaxing the numpy pin to `>=1.26,<3`. A new golden-fixture suite gates future scoring changes against real unmocked Stickler, with `scripts/regenerate_evaluation_goldens.py` as the sanctioned way to update goldens. Docs sweep across both tiers ([docs/evaluation.md](docs/evaluation.md), `evaluation-enhanced-reporting.md`, `test-studio.md`, `evaluation/README.md`, `step7_evaluation.ipynb`). Live-validated: 50/50 single-section docs (OmniAI OCR benchmark) and 50/50 multi-section docs (DocSplit-Poly-Seq, 214 sections).

- **Extensions UI: one honest subscription banner, and a route to where a subscription is actually managed.** The feature page could render three host banners that contradicted each other — a yellow "Subscription not verified" above a green "Subscription active · Source: advisory", plus a version banner that also mentioned the subscription. There is now exactly one: verified subscriptions get **Subscription active (AWS Marketplace)** with a **Manage subscription** button pointing at the buyer's [AWS Marketplace subscriptions console](https://console.aws.amazon.com/marketplace/home#/subscriptions) — the only place a real subscription can be changed or cancelled — while an unverified grant states what actually happened ("Access allowed without a verified subscription · source: `<source>`") and names the raw source, because there the exact mode is the point. The version banner no longer mentions subscriptions, the pre-install screens no longer claim "Your AWS Marketplace subscription is active" off the back of an `auto` / `advisory` / `simulated` grant, and **OSS extensions no longer show a "Subscription active" banner with a Cancel button** `unsubscribeFeature` has no way to honour. The nav badge now says **Install** for anything not installed, OSS and marketplace alike, instead of inferring "not subscribed" from "not installed" — a verdict the nav cannot check without putting a real Marketplace API call on every page render — with the hover text carrying *"Requires an AWS Marketplace subscription"*, a property of the extension rather than a claim about this customer. The info panel drops the "Preview / future capability / no paid extensions yet" copy and the three `(future)` qualifiers, reorders the lifecycle list Subscribe → Install → Ready → Update, and notes that Marketplace extensions are published per Region. `FeaturesToolsPanel` and the nav module had no tests; they have eight between them now, mutation-verified against the shipped behaviour.

- **The Marketplace seller-template presign is gone; the template URL is a bare public S3 URL.** It could never have protected the artifacts it appeared to protect: the registered Quick Launch template URL is fetched by AWS Seller Ops during listing review and by CloudFormation in an arbitrary buyer account, and the Lambda code zips are fetched from the buyer's account at deploy time — all of which require public read. Meanwhile its expiry was a real failure mode, 403-ing an admin who sat in the CloudFormation wizard longer than `PRESIGN_TTL_SECONDS`. Removing it collapses the marketplace and OSS launch paths into one code path and drops the requirement that every customer configure `SellerBucketObjectArns` plus a seller bucket-policy grant just to click Launch. The entitlement check on the Launch button is now documented for what it always effectively was — an **advisory UX gate** that routes an unsubscribed admin to Subscribe; the commercial gate is the Marketplace subscription plus the extension's own runtime check, never artifact secrecy. `SellerBucketObjectArns` survives with a narrower purpose (signed `latest.json` reads from a private self-published artifacts bucket).

- **The synthetic generator's render stage gets a longer, configurable timeout.** The per-stage cap was 1 hour and not reachable from a stack parameter, so it silently used its default. The cap applies per *stage*, and one stage renders a whole batch, so its cost scales with count × quality × augmentation rather than per document — and because the stage is all-or-nothing, exceeding it discards every document generated so far. Default raised to 3 hours and exposed as `SeedNodeTimeoutSeconds` (600–27000) on the Test Set Generator extension. The whole-pipeline timeout is now always at least twice the per-stage one however it is configured, since the two are easy to misorder and the failure is silent.

- **One image pane for both document editors.** `VisualEditorModal` (human review) carried a 240-line inline reimplementation of the shared `PageImageViewer` — its own presigned-URL loader, `BoundingBox`, zoom/pan handlers and a copy of the auto-centering calculation — so every fix to the image pane had to be made twice. It now uses the shared component: 679 lines of duplication removed, and the file drops from 1,986 to ~1,290 lines. `zoomToField` was already implemented but exposed only via a DOM ref nothing read; a new `zoomToFieldOnFocus` prop makes it a real capability, defaulted **off** so the ground-truth editor keeps highlighting-without-zooming while human review keeps zooming to the field being edited.

### Fixed

- **Upgrading a commercial stack could wedge in `UPDATE_ROLLBACK_FAILED` on `iam:UpdateAssumeRolePolicy` AccessDenied ([#632](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/632)).** v0.6.2 added the two GovCloud Cognito federated principals to `CognitoAuthorizedRole`'s trust policy unconditionally. They are inert for STS at runtime but *not* at deploy time: changing the trust-policy document on an existing role is a separate `iam:UpdateAssumeRolePolicy` call, so any pre-v0.6.2 → v0.6.2+ upgrade under a CloudFormation service role or permissions boundary lacking that action failed mid-update — and the rollback needs the same action, so the stack could not self-recover. Fresh deploys were never affected. The GovCloud statements are now gated on an `IsGovCloudPartition` condition, so commercial renders the byte-identical pre-v0.6.2 document (no diff → no API call). ⚠️ **Recovery for a wedged stack:** `continue-update-rollback --resources-to-skip <StackName>-CognitoAuthorizedRole`, then upgrade to this release. Two follow-ons: the shipped `IDP-Cloudformation-Service-Role.yaml` now also grants `iam:UpdateAssumeRolePolicy`, `iam:PutRolePermissionsBoundary` and `iam:DeleteRolePermissionsBoundary` (it granted none, so our own documented service role hit this too, and without them any future trust-policy or boundary change would wedge again); and `scripts/sdlc/validate_service_role_permissions.py`, the CI check that should have caught this, **had been passing vacuously** — its IAM comparison crashed on the first `Fn::If`-wrapped inline policy and a broad `except` returned an empty action set, so "no missing permissions" meant "nothing was compared". It now walks intrinsics tolerantly, fails loudly on an unparseable template, and additionally derives the IAM control-plane actions CloudFormation needs to manage the roles our templates declare.

- **Textract signature detections were described only by raw coordinates, and both the extraction and the confidence model read them backwards ([#634](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/634)).** Surfacing the detection was necessary but not sufficient: given `signature 1: confidence=11.0 left=0.572 ...`, the confidence model concluded *"the OCR detected a signature region in the first (left) signature box"* and, of the cell that actually held the mark, *"the OCR did not detect any signature in this region"* — exactly inverted, on a form with two identically-labelled signature cells. Attributing a box to a field is spatial arithmetic that models get wrong, and the inline `[SIGNATURE]` token made it worse: reading order placed it immediately after taxpayer 1's date while the detection sat in taxpayer 2's cell, and the block's own wording ("matching the inline [SIGNATURE] token(s) above") reinforced that false association. Each detection is now reported with a confidence **band** (`11.0 (very low)`), its position in the left/right + upper/lower language field descriptions actually use (`right half, lower area (x=59%, y=89%)`), the OCR text it **sits on** or beside (`at: "Signature of taxpayer"; right: "Date"`, derived from existing LINE geometry, skipping single-character OCR noise), an explicit region total, and a caveat that the inline token's placement is not evidence of field membership. **Measured against the issue's document** (Sonnet 4.5, temperature 0, page images attached, repeated runs): the enrichment alone does *not* fix the extraction, nor do strict field descriptions alone, nor few-shot examples alone — all three together do, reproducibly. Notably, with descriptions + examples but *without* this enrichment the false positive does not disappear, it **migrates to the other taxpayer's field**: the model can tell a mark exists but not which cell owns it. `docs/configuration.md` now documents that recipe. Eleven new tests, mutation-verified.

- **Uninstalling a feature that owns a `preprocessing` hook no longer fails every subsequent document.** A flat point's hook is invoked **by ARN**, so an uninstalled feature's ARN left behind in the active config version names a Lambda that no longer exists — and the PII Anonymization extension's shipped preset sets `onError: fail`, which makes the dispatcher raise and the workflow land in its terminal `PreprocessingHookFailed` state. Every document processed after such an uninstall would fail, with no signal beyond a `ResourceNotFoundException` in the dispatcher log, until an admin hand-edited the configuration. `unregisterFeatureHooks` now disables the section and nulls the ARN when the requesting feature is the recorded owner — the fail-safe end state — while leaving `args` in place so a re-install is a one-field change, and the clear is scoped to the owner so uninstalling one feature cannot disable another's hook. Note this is a **behavior change to feature teardown**: `unregisterFeatureHooks` was previously a no-op for the flat points. Relatedly, the dispatcher's hook-list field was chosen by testing whether the point name started with `pre` — a heuristic that would also have mis-read the new `postprocessing` point — so flat points are now an explicit set shared by the dispatcher and the registration resolver, and the documented-but-never-read `<section>.preHook` list is gone.

- **The evaluation report described every failed section as a missing configuration, whatever had actually gone wrong ([#628](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/628)).** The ⚠️ EVALUATION FAILED block asserted "no configuration was found for document class **X**" and printed the matching "How to fix" list unconditionally — so an unparseable extraction response, a baseline type mismatch, or an empty nested object all sent the reader off to add a config entry that already existed, while the real cause sat further down under "Detailed error". `evaluate_section` now tags each failure with a `failure_type` and the report leads with the actual reason and shows remediation matching *that* cause; a results file written before the field existed shows the reason with **no** "How to fix" list, deliberately, since guidance for the wrong cause is worse than none. The zeroed "Metrics (Failure State)" table now also says the zeros mean *not scored*, not *scored zero*. See [docs/evaluation.md](docs/evaluation.md#when-a-section-fails-to-evaluate).

- **A discovered document class whose name contained a space broke BDA sync for every such class ([#624](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/624)).** Discovery can emit class ids like `Task cards`, and the id was composed verbatim into a blueprint name — but `CreateBlueprint` requires `[a-zA-Z0-9-_]+`, so every affected class failed with a raw `ValidationException` (`Succeeded: 0, Failed: 3`) and the config version was left holding a BDA project with **zero** blueprints. Fixed on both sides: **Discovery** normalizes the id at its single write path (`Task cards` → `Task-cards`), keeping the readable original in `description`, and the multi-section auto-detect prompt no longer suggests a spaced label; **BDA sync** sanitizes at all four sites that compose or match a blueprint name — create, lookup, orphan cleanup and the optimizer's own create path — because sanitizing at only some is worse than none (lookup would stop finding what it created, orphan cleanup would delete a live blueprint). `sanitize_class_name` deliberately **preserves underscores** rather than reusing the project-name sanitizer, so any id that already worked renders byte-identically and no existing blueprint is orphaned. The upgrade path is handled: re-discovering a class saved with the spaced spelling *replaces* the stale entry rather than adding the normalized id beside it, which would leave two classes fighting over one blueprint; only an id that normalizes to the class being written is replaced. A class id with nothing usable in it now fails with an actionable message, a failed class carries **why** it failed through to the UI, and the rule lives in one place (`idp_common/config/class_names.py`) shared by Discovery, BDA sync and the optimizer, matching what the Schema Builder already enforced. See [docs/discovery.md](docs/discovery.md#class-name-normalization). Reported with full root-cause analysis by [@saygenie](https://github.com/saygenie).

- **`config-validate` rejected every GovCloud config, and every config that names a model by ARN ([#648](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/648)).** The model-ID check compared the configured value to the `pricing.yaml` catalog by exact string match. GovCloud Bedrock has no bare cross-region model-ID path — a config there must name an account-scoped inference-profile ARN — so the two forms could never match and all four sections failed at once. Not gated by `--strict`: it set `valid = False`, so it also failed `config upload`, where `--no-validate` was the only way past. The same miss hit commercial `foundation-model/…` and provisioned-throughput ARNs. An ARN is now reduced to the model ID it names before the catalog lookup; an ARN whose resource is opaque (`application-inference-profile/<uuid>`) yields a **warning** rather than an error, since it is unverifiable rather than wrong, while a bare model ID that does not match is still an error. Reported with a diagnosis and a proposed patch by [@karolytanczos-govcio](https://github.com/karolytanczos-govcio).

- **The confidence pass ran blind on the default settings: page images were dropped whenever `geometry.mode` was `ocr_only` (the default) or `off`, so a `{DOCUMENT_IMAGE}` placeholder rendered as an empty block ([#634](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/634)).** The gate assumed a confidence pass not asked for bounding boxes has no use for the page image — true for text-transcription fields, wrong for every field whose evidence is *visual*: signature / checkbox / stamp booleans, handwriting, struck-through values. Those are exactly the fields most likely to be extracted wrongly; observed on a signature-presence boolean that the model marked `true` on an unsigned form and then rated 0.85, its own reason conceding it was "without being able to visually verify an actual signature mark". It also silently overrode the user's prompt and contradicted the documented behavior. Attachment is now decided solely by the prompt, as the extraction and sharded agentic paths already behaved. ⚠️ **Cost note:** on a config that keeps `{DOCUMENT_IMAGE}` in the confidence prompt (the shipped default), the confidence request now carries ~1.7K additional input tokens per page; remove the placeholder from `extraction.confidence.task_prompt` to opt back into a text-only pass.

- **The confidence prompt showed no field descriptions at all for any class whose groups or lists live in `$defs` — which is every class the UI's schema editor produces ([#634](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/634)).** `_format_property_descriptions` read `type` and `description` directly off each property, but a `$ref` property carries neither — both live in the referenced `$defs` entry — so a class with `"Signatures": {"$ref": "#/$defs/Signatures"}` rendered as literally `Signatures  [  ]`, with neither the group description nor its members'. The confidence model was therefore asked to judge values whose meaning it had never been told, which matters most for fields whose *definition* is the whole question (a boolean "is a signature present?" scored 0.85 on a document with no signature). Subschemas are now dereferenced — chains followed, sibling `description` overriding the definition's, cycles and dangling refs degrading rather than raising — at every level the formatter walks. Extraction was never affected, since it passes the raw schema.

- **Test Studio scored no-op documents as `0.000`, dragging the run-level Average Weighted Overall Score toward zero.** Sections whose class has no extractable schema (class not in config, or class with zero attributes) landed in `evaluate_section`'s failure branch and emitted `weighted_overall_score: 0.0`, producing a spurious `0.0–0.1` histogram spike and `0.000` rows in "Documents with Lowest Weighted Overall Scores". Detection now happens before scoring and returns `evaluation_skipped=True` / `weighted_overall_score=None`; the document aggregator drops those sections from the weighted mean and confusion-matrix rollup and folds them into `excluded_sections`, so the markdown "Excluded Sections" table covers them alongside `x-aws-idp-exclude-from-processing` and the histogram and lowest-scores views exclude them automatically. The count is surfaced end-to-end as an **"Excluded Docs"** KPI tile beside Overall Accuracy / Avg Weighted Score / Duration, with an info icon explaining why those documents weren't scored, hidden when nothing is excluded. Existing cached runs trip the staleness guard once and re-aggregate.

- **One blank cell in a table excused the whole column from review.** Absent fields are excluded from `minConfidence` / `alertCount` because a blank W-2 box scores 0.0 legitimately — but the exclusion was keyed on the bare leaf field name, while confidences were matched on the same bare name. A single empty `Description` in a 200-row transaction table therefore excluded *every* `Description` score in the document: measured on a three-row fixture, a document with two genuinely weak descriptions reported **0 alerts instead of 2**. Both walkers now build path-qualified keys (`Transactions[1].Description`), so they align per occurrence. This understated review need on exactly the table-heavy documents the feature targets.

- **Dependabot security bumps to the multi-doc discovery container never reached the built image.** The nested stack's CodeBuild buildspec heredoc'd its own inline copies of `requirements.txt` and the `Dockerfile`, so the checked-in files were dead. The two forked silently: Dependabot bumped the on-disk `requirements.txt` to `Pillow==12.3.0` (PR #530) while the image went on installing `Pillow==12.1.1` — a merged CVE patch that never shipped. Compounding it, two rebuild gates also ignored those files: neither was in the component's smart-rebuild dependencies, and — the load-bearing one — the `BuildHash` that is the *sole* trigger for re-running the container build hashed only `src/lambda/multi_doc_discovery`, so on an in-place stack update CloudFormation saw no change and the ECR image kept the vulnerable dependency. (A fresh create always builds, which is why end-to-end create testing never saw it.) The buildspec now builds `-f nested/multi-doc-discovery/Dockerfile` from the source zip, `publish.py` includes both files and fails loudly if either is missing, both feed `BuildHash` and the component dependencies, and tests assert the buildspec contains no inline pins and that `BuildHash` actually moves when either input changes.

- **S3 buckets created by the CLIs and by the sample notebooks had no `EnforceSSLOnly` bucket policy, so they accepted plain-HTTP requests.** All 13 buckets in `template.yaml` deny `s3:*` when `aws:SecureTransport` is false, but the buckets created *imperatively* outside CloudFormation never got the equivalent. Three paths are fixed via a new shared `idp_sdk._core.s3_security` helper — the `idp-cli publish` artifacts bucket, the `idp-cli deploy` config-staging bucket (hardened on **reuse** as well as creation, since `get_or_create_config_bucket` returns early on a prefix match) and `idp-feature-cli`'s `ensure_artifacts_bucket` — plus `s3-sourcecode.yml`'s `InitialInstallBucket` and the 11 sample notebooks' `ensure_bucket_exists`. The statement is applied **additively** and idempotently, preserving existing statements and using the region's real partition, and handles **both** forms the IAM grammar allows for `Statement` (a single object as well as an array) — iterating the single-object form would have replaced an operator's statement with the strings `"Sid"`, `"Effect"`, …; the pre-existing public-read merge path is corrected for the same latent flaw. Unlike Block Public Access, which the publisher never touches on a bucket it didn't create, this only ever *tightens* access, so it is applied to pre-existing buckets too, best-effort there. ⚠️ **Note:** running the sample notebooks now requires `s3:PutBucketPolicy` on the buckets they create. See [lib/idp_sdk/README.md](lib/idp_sdk/README.md#buckets-the-cli-creates) and [lib/idp_feature_sdk/README.md](lib/idp_feature_sdk/README.md#artifacts-bucket-security).

- **Presigned URLs served uploaded HTML and SVG inline.** `text/` and `image/` are renderable prefixes, so an uploaded `evil.html` or `evil.svg` was served with `Content-Disposition: inline` and executed script on the bucket origin. Those types are now forced to `attachment` — explicitly, because omitting the disposition lets the browser decide from `Content-Type` alone and `text/html` still renders. Separate origin from the app and an authenticated uploader is required, so this is defence in depth rather than a live hole.

- **Transient Bedrock streaming errors were not retried, because the same condition is spelled differently by the streaming API.** `ConverseStream` reports `internalServerException` where `Converse` reports `InternalServerException`, and the retryable-error set was matched case-sensitively, so a transient server-side fault on a streaming call (agent chat, streaming summarization) failed on the first attempt while the identical fault on a non-streaming call retried with backoff. Matching now folds case in both the sync and async decorators, and the synthetic generator retries the same faults beneath SEED and Strands. **Behavior change beyond the bug:** the *sync* decorator previously had its own hardcoded list of four retryable codes and now shares `DEFAULT_RETRYABLE_ERRORS` with the async one, so every synchronous Bedrock call also retries quota, service-unavailable, internal-server and read-timeout conditions. Those are genuinely transient and the divergence was an oversight, but the effect is that a call which used to fail fast on e.g. `ServiceQuotaExceededException` now spends its retry ladder first: expect higher tail latency in exchange for fewer hard failures.

- **"Update available" no longer offers a downgrade.** `updateAvailable` was computed as `latestVersion != installedVersion`, so any difference lit the badge — including a catalog *behind* the installed version, telling an admin running v0.1.1 that v0.1.0 was "available". That state is routine: `idp-feature-cli deploy --from-code` (the documented dev loop) installs a newer extension immediately, while `catalog.json` only refreshes on a host stack create/update. The resolver now compares parsed SemVer and requires strictly newer — numerically (`0.1.10` > `0.1.9`, which string comparison gets backwards) and with SemVer §11 prerelease precedence — falling back to the old inequality with a warning for non-SemVer strings, and the feature page now trusts that flag instead of recomputing it with plain inequality and resurrecting the bug.

- **Test Studio reported "N/K completed" where N > K when the S3 test-set folder had drifted past the test set's declared `fileCount`.** Reported as `85/10 completed` and `2005/5 completed`. The runner wrote `FilesCount = test_set.fileCount` into the run metadata while the copier listed *everything* under the test set's `input/` prefix — nothing capped the listing when `numberOfFiles` wasn't in the SQS message — so the copier stamped a record for all 85 / 2005 objects and the status resolver counted every one as completed against the unchanged denominator. The runner now always sends `filesToProcess`, and the copier caps the listing at that count after a deterministic sort (so repeat runs pick the same subset), warns loudly when the folder exceeds the declared count, and falls back to `numberOfFiles` for messages enqueued before the field existed.

- **A test set could show "Generating…" forever.** The generator extension writes `GENERATING` onto the host test-set record and its runtime is what clears it, so a runtime that dies mid-run — or an extension that is uninstalled — left the status with no owner, rendered as an in-progress spinner that survived reloads and redeploys. The host resolver now fails a non-terminal test set whose status has gone stale, with per-status windows because plausible durations differ by orders of magnitude (a synthetic generation legitimately runs for hours; a file copy does not). Deliberately in the host rather than the extension, since the point is to survive the extension being absent. Records written before the status timestamp existed are left alone rather than presumed dead, and the write is conditional so a job reporting in mid-flight is not clobbered. The generator's job record is also seeded with a heartbeat when the processor accepts the job — so a container that never starts is reapable rather than invisible — and reaping a job now releases the host test-set record too. The generator heartbeats its own record with elapsed minutes, so a stage that completes no documents (augmentation, most obviously) is no longer indistinguishable from a dead runtime.

- **"View Source Document" downloaded the file instead of showing it in the viewer — for generated test sets only.** A set created by the [Test Set Generator](docs/extensions/idp-data-generator.md) rendered nothing in the viewer; a zip-uploaded set on the same screen worked, which made it look like a viewer bug. Two independent causes: the generator's `upload_file` left the object at S3's `binary/octet-stream` default, and `getFilePresignedUrl` set no response-header overrides, so the stored type passed through verbatim. The resolver now always sets `ResponseContentType` from the type it already corrects by extension, and adds `ResponseContentDisposition: inline` for the types a browser can display (PDF, `image/*`, `text/*`) — deliberately *not* for spreadsheets and Word documents, which should keep downloading. Overriding at presign time also repairs documents already stored with the wrong type.

- **Generated documents were rejected in a loop because the schema forbade what its own field descriptions instructed.** IDP config classes are written for extraction, where the convention for a field the document does not contain is an explicit null — many descriptions say "Output null if not shown". Handing that schema to the generator made the instruction unsatisfiable: a JSON Schema `string` cannot hold null, so the generator obeyed the description, validation rejected the document, the critic bounced it, and it retried until the render stage's budget was gone and the whole batch was lost (measured on one 11-document run: 119 critic rejections, 138 instances of that single contradiction). The generator's copy of the schema now widens optional scalar leaves to accept null — resolving it in the direction the config author intended rather than rewriting descriptions the accelerator does not own. Required fields stay strict, objects and arrays are not widened, and an `enum` on an optional field gains `null` too, since an enum constrains the value independently of `type`. Extraction is unaffected.

- **A settings-cache test failed only on CI runners with 50–300s of uptime ([#609](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/609)).** `test_ssm_failure_on_stale_cache_defers_full_ttl` asserted on a *derived age in seconds* after setting `_cache_time = 0.0` as an "expired" sentinel — but that sentinel reads as "the boot instant", so the derived age was really the runner's uptime, and the assertion held only below 50s or above the 300s TTL. The test now drives an injected fake clock and asserts on retry **behaviour** rather than a wall-clock age, passing identically at every simulated uptime. Three tests added alongside for paths the rewrite made testable.

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.6.5.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.6.5.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.6.5.yaml`
  

## [0.6.4]

### Added

- **A `postprocessing` pipeline hook, symmetrical with `preprocessing`, as the workflow's final optional step — and it can modify the document.** The existing `PostProcessingLambdaHookFunctionArn` stack parameter was a fundamentally different mechanism: an EventBridge rule firing *after* the execution, invoking a customer Lambda asynchronously with a document *snapshot* it could not change, gate, or swap by activating a config version. There is now a real `postprocessing` extension point on the shared workflow tail — after evaluation, before the terminal state, so it fires in both BDA and pipeline modes — that mirrors `preprocessing`: the same standalone top-level config section holding a single flat hook (`enabled` / `arn` / `onError` / `args` / `featureId` / `allowDocumentUpdate`), the same dispatcher, the same `updatedDocument` mutation contract and guardrails, and the same `idp_common.hooks` helpers. It appears in the View/Edit Configuration UI as **Postprocessing**, directly below Evaluation. Because it sits at the end of the tail, a mutation there is the last word on the document: it reaches the tracking row, the reporting/Athena rows, and the UI. Three deliberate asymmetries with `preprocessing`, all following from the document already being fully processed: `onError` defaults to `continue` (`fail` would mark an otherwise-successful document FAILED for a delivery-integration error), `halt` is not actionable so the dispatcher logs it and reports `haltIgnored: true`, and a mutation cannot set a terminal `status`. The hook is **not** skipped while a HITL review is pending — it receives the document's `hitl_status` / `hitl_triggered` / `hitl_sections_pending` fields and decides for itself, which means it fires once on the interim completion and again after review, so hooks here must be idempotent. Both `preprocessing` and `postprocessing` are now registrable by Feature Platform features via `registerFeatureHooks` — `preprocessing` was listed in the feature manifest schema but rejected by the resolver, which is why the PII Anonymization extension had to bake its ARN into a config preset instead. Each flat point holds exactly one hook, so a registration that would overwrite another feature's hook is refused rather than silently disabling it, and a feature's preset `args` survive registration. Inert by default. See [docs/feature-platform.md](docs/feature-platform.md#pipeline-hooks) and the [developer guide](docs/feature-platform-developer-guide.md). (#611)

- **`allowDocumentUpdate` is now editable in the Configuration UI.** The per-hook kill-switch that pins a hook to observe-only was enforced by the dispatcher and documented for admins, but was absent from the config schema — so the UI could not set it. Added to the `preprocessing` and `postprocessing` sections. (#613)

- **End-to-end CI coverage for the pipeline-hook mechanism (Step 14).** None of the hook points had a live test — unit tests exercise the dispatcher with fakes, but four things only exist on a real stack: the dispatcher reading a hook out of a real config version, `UpdateSchemaConfig` publishing the hook config sections, a hook Lambda clearing the dispatcher's IAM condition, and a hook's `updatedDocument` surviving into the *persisted* document. That gap mattered because a hook which silently never fires is indistinguishable from one that ran and decided nothing — which is exactly how [#599](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/599) shipped. The new step deploys a real hook Lambda built from the published `idp_common.hooks` helpers and tagged `idp:feature-id` (the ABAC path installed features use), registers it at both `preprocessing` and `postprocessing` in a dedicated, never-activated config version, processes a document pinned to it, and asserts the hook sections survived the config round-trip, that the hook was invoked at both points, that its update was *accepted* rather than refused by the guardrails, and that the marker reached the tracking row. Tears down its Lambda + role in a `finally`, since a leftover hook ARN fails every subsequent document. See [CI test coverage → Step 14](scripts/sdlc/docs/CI_TEST_COVERAGE.md). (#612, #614, #615)

- **Auto Optimizer documentation: architecture diagram, demo videos, and the EMNLP paper.** The README now carries the IDP Auto Optimizer diagram and a link to the published paper, and the [auto-optimizer guide](docs/auto-optimizer.md) has demo video walkthroughs.

### Changed

- **The configuration version is now pinned once, at the workflow entry point, instead of being re-resolved by each consumer.** `queue_processor` — the single chokepoint every document execution passes through — now stamps `document.config_version` before compressing and starting the Step Functions execution, using a new `ConfigurationManager.resolve_active_version()`. Previously the pin was set only when the uploader supplied `config-version` S3 metadata or when the queue sender managed to resolve it, so a document could reach the workflow unpinned and each downstream consumer then resolved the active version independently — every one of those a place the answer could disagree or silently fail (which is exactly how #599 below presented). Pinning also guarantees the state machine's `use_bda` / `bda_project_arn` routing flags are read from the *same* version as the rest of the pipeline. Deliberately **not** a new failure mode: an existing pin is never overwritten (an upload-time choice, or a version carried through a HITL reprocess, still wins), and neither "no version is active" nor a DynamoDB failure fails the document. The dispatcher's own scan is retained as a defensive fallback for documents queued by an older release.

- **`make lint` now fails the build on any DynamoDB filtered `Scan` that cannot see all its matches (`make check-filtered-scans`).** The defect fixed below has appeared seven times across the platform with symptoms as unalike as "configuration appears empty", "a list view shows fewer rows than exist" and "pipeline hooks stop firing", so it is now enforced rather than re-reviewed. An AST pass flags each `.scan(FilterExpression=...)` whose enclosing function never references `LastEvaluatedKey`, resolving splatted keyword arguments, and reports whether the call is bounded by an explicit `Limit`. Where a bounded sample is genuinely intended, an inline `# filtered-scan-ok: <reason>` marker suppresses it — the marker requires prose after the colon, so silencing the check leaves a reviewable justification. Wired into `make lint`, `make fastlint` and `make lint-cicd`.

### Fixed

- **Pipeline hooks silently stopped firing as the ConfigurationTable grew, and three other places resolved the wrong configuration version for the same reason.** Four Lambdas located the active configuration version with a filtered DynamoDB `Scan`. DynamoDB applies both `Limit` and the implicit 1MB page size to the items it **examines**, not the items matching `FilterExpression`, so a single scan finds nothing whenever the active `Config#` row sorts beyond that window — which gets likelier with every configuration version saved and every feature installed. None of the four paginated. The failures were silent and growth-triggered, so they strike long-lived production stacks hardest, and they depend on hash distribution, so two stacks running identical software behave differently. Observed live on a v0.6.3 stack with 35 config rows: the active row sat at scan position 33 behind a 10-item examine window, so **every registered pipeline hook stopped being invoked** — the workflow still succeeded, the document still reached `COMPLETED`, and the hook Lambda wrote no log entries at all. All four sites now page until they match, and each projects only the key attribute so the examine window is not spent reading whole config bodies: the **pipeline-hooks dispatcher** (hooks stop firing), **`registerFeatureHooks`** (hooks written into a non-active version, so registration reports success and they never run), the **queue sender** (the document is processed under the *default* configuration rather than the active one), and the **Test Studio test runner** (a run's comparisons are scored against a configuration the documents were not processed under). Two diagnosability fixes alongside: failing to resolve an active version now logs at **WARNING**, worded distinctly from the ordinary no-op of a host with no features installed, and the dispatcher now reports `configVersion` in its **no-hooks** result too — `invoked: 0` alone cannot distinguish "the active version has no hooks here" from "we resolved the wrong version". See [Feature Platform developer guide → Verifying a hook actually fired](docs/feature-platform-developer-guide.md). (#599)

- **Three more unpaginated filtered scans, found by the new lint check.** All silent, all growth-triggered. (1) **The admin user list was truncated to the first page** — an admin simply saw fewer users than exist, with no error and no indication the list was partial. (2) Worse, `sync_cognito_users_to_dynamodb` built its "already in DynamoDB" email set from one page, so **every user beyond it looked absent and was re-created under a fresh `uuid4`**, duplicating the record on each sync. (3) BDA project deletion's `BdaProject#` tracking cleanup could **miss the very row it was deleting**, leaving an orphaned tracking entry.

- **Policy Discovery had four ways to quietly do less than asked.** Each ended in the same place: a job reporting success while rule validation produced nothing, or silently less than the policy required.
  - **A ruleset truncated at the model's output-token limit was persisted as if complete (#603).** Extraction validated *structure*, not *completeness*, so a response cut off after rule 60 of 140 that happened to close its braces passed every check — the job logged success and 80 rules were absent from the compliance validation set with nothing saying so. The traditional path now checks the model's stop reason, retries, and **fails the job** rather than saving a knowingly-partial ruleset; the agentic path's opaque "unrecoverable state" error is translated into the same actionable message, so both name the same remedies (split the document, or raise `discovery.rules.max_tokens`).
  - **The documented output shape was not the shape that works (#600).** `discovery_rules_from_document_local` returned raw LLM output keyed on `x-aws-idp-rule-type`, while the S3 path reshaped it to `policy_classes` / `x-aws-idp-policy-type` — the only keys the runtime and the Policy Schema UI read — so a notebook user following the documented steps saved rules under a config key nothing reads. Both paths now share one `to_policy_class()` reshape, the legacy `rule_classes` key logs a WARNING when discarded instead of vanishing silently, and readers accept the legacy discriminator as a fallback in both the classifier and the rule-question lookup.
  - **A second discovered policy class silently disabled rule validation for all of them (#601).** Discovery never sets a matching regex, and validation requires one as soon as there are ≥2 classes, so uploading a second policy manual switched off the *first* policy's rules too, with no signal beyond a WARNING in a downstream Lambda log. The save that creates that state now warns, and the warning reaches the discovery job's status message in the UI. It covers both bad states — a class with no regex alongside classes that have one is never evaluated while the others work. The runtime's fail-closed behavior is deliberately unchanged and now pinned by a test.
  - **Rules duplicated across policies were invisible until the bill arrived (#602).** Rule Validation answers each copy with its own LLM call for every document, so a rule stated in three overlapping payer manuals is a standing 3× multiplier — and the differently-worded copies can return contradictory Pass/Fail answers. Saving now reports incoming rule names that already exist in other policy classes, in the log and in the job status message.

  Deliberately out of scope, and still documented as limitations: multi-document clustering / `policy_group`, LLM-proposed regexes, and automatic dedup. (#600, #601, #602, #603)

- **The optional post-processing Lambda hook silently never fired for large documents.** The `PostProcessingDecompressor` inflates the compressed document and hands it on with `InvocationType='Event'`. An **asynchronous** Lambda invoke caps its payload at **1 MB** — the 6 MB limit applies only to synchronous invokes — so any document whose decompressed JSON exceeded that raised `RequestEntityTooLargeException`, which became 3 EventBridge retries and then the DLQ. The hook therefore failed for exactly the big multi-section packets most likely to matter. The decompressor now measures the payload and, when it will not fit, invokes the customer Lambda with the **original compressed event** instead of failing — with a warning naming the size and `sentCompressedFallback: true` in the response. Documents under the limit still arrive decompressed. Documented, since a hook handling arbitrarily large documents must now check `document.compressed`.

- **The post-processing hook fired for PII-redacted originals it should never have seen.** When a `preprocessing` hook halts a document after spawning a redacted copy (the PII Anonymization extension's "redact copy and stop" mode), that execution still ends as `SUCCEEDED`, so the EventBridge rule matched and the customer's post-processor was invoked with the **un-redacted original** — precisely what the redaction existed to prevent. The status lives inside `detail.output`, which an EventBridge pattern cannot filter on, so the decompressor now checks it and skips the invocation for `REDACTED_SUPERSEDED`. The redacted copy is processed as its own document and fires the hook normally.

- **Uninstalling a feature that owns a `preprocessing` hook no longer fails every subsequent document.** A flat point's hook is invoked **by ARN**, so an uninstalled feature's ARN left behind in the active config version names a Lambda that no longer exists — and the PII Anonymization extension's shipped preset sets `onError: fail`, so the dispatcher raises and the workflow lands in its terminal `PreprocessingHookFailed` state. Every document processed after the uninstall failed, with no signal beyond a `ResourceNotFoundException` in the dispatcher log, until an admin hand-edited the configuration. `unregisterFeatureHooks` now disables the section and nulls the ARN when the requesting feature is the recorded owner — the fail-safe end state — while leaving `args` in place so a re-install is a one-field change, and the clear is scoped to the owner so uninstalling one feature cannot disable another's hook.

- **A completed test run reported `EVALUATING` forever — alongside `10/10 processed` and `0 evaluating` — whenever the test set name contained a space or parenthesis.** The run had genuinely finished and Stickler had already computed its accuracy, but the aggregate metrics were never cached, and `get_test_run_status` deliberately reports `EVALUATING` while a terminal run has no `testRunResult`. The metrics were destroyed by a **context confusion in the SQL-injection guard**: `test_run_id` is derived from the user-chosen test set name and interpolated into a string *literal* (`WHERE document_id LIKE '{test_run_id}%'`), but was checked against the identifier allow-list `^[a-zA-Z0-9_\-./]+$` — so a legitimate name like `ConfBench (light noise)` raised `test_run_id contains invalid characters`. The correct defense for a literal is escaping, so it now goes through a `_sql_like_prefix()` helper that doubles `'` and neutralises the `%`/`_` wildcards via a paired `ESCAPE` clause — the latter fixing a second latent bug where a name containing `_` silently widened the prefix match to other runs. The database name keeps the strict allow-list. Two failure-containment bugs are fixed alongside, either of which alone would have prevented the permanent stall: the Athena calls for split-classification metrics and cost are only *supplements* to metrics Stickler has already computed, yet an exception from them discarded the good Stickler results (and logged the misleading `Test execution aggregation Lambda failed`) — they are now best-effort and degrade to their documented "no data" shapes; and **nothing re-enqueued the aggregation**, so a run whose single attempt failed stayed metric-less permanently. Both `get_test_run_status` and `get_test_results` now re-enqueue when a terminal run has no metrics, throttled by an atomic conditional write that also collapses the redundant duplicate aggregations observed during the incident. (#619, #620, #621)

- **`idp-cli status` and `run-inference --monitor` crashed for any document in one of four real processing states.** The SDK's `DocumentState` enum validates the `ObjectStatus` read straight out of the tracking table, and it had drifted behind `idp_common.models.Status` — so a document in a state the enum did not know about aborted monitoring with `1 validation error for DocumentStatus` instead of reporting progress. Two of the four are on entirely ordinary paths: **`PREPROCESSING`**, which the pipeline-hooks dispatcher sets for *every* document whenever a preprocessing hook is registered (so every [PII Anonymization](docs/extensions/pii-anonymizer.md) user hit this on every document), and **`RULE_VALIDATION_POLICY_CLASSIFICATION`**; the others are `REDACTED_SUPERSEDED` and `PENDING_UPLOAD`. Compounding it, `--monitor` swallowed the error and still exited 0, so a batch looked like it had been waited for when it had not. All four are now present, and a new test asserts the enum is a **superset** of the runtime `Status`, so a future addition fails offline in CI rather than in a user's CLI. Two related monitor bugs fixed alongside: `REDACTED_SUPERSEDED` is now treated as terminal (monitoring previously waited for it until timeout) and counted as done rather than failed, and the progress display's "running" bucket is now derived rather than an explicit list that omitted `OCR`, `PREPROCESSING`, `POSTPROCESSING` and `RULE_VALIDATION_POLICY_CLASSIFICATION` — documents in those states were shown as **Queued**.

- **The Configuration editor raised two identical "Configuration saved successfully" banners, and neither went away.** A save produced both an app-wide green toast and a second, near-identically worded inline Alert above the tabs, and both were still on screen after the user's next edit raised the blue "You have unsaved changes" banner — one saying the work was persisted and the other saying it was not. The inline Alert is removed, keeping the toast (which stays visible when the user is scrolled deep into a long config form). **The toast now actually auto-dismisses**, which is a fix in `useNotifications` rather than in the config page: it always set a 5s dismissal timer but also reset `successMessage` as it fired, and since that value is in the effect's dependency array the immediate re-run's cleanup cleared the timeout it had just set — so *every* success toast in the app lingered until dismissed by hand. Pending timers now live in a ref cleared on unmount. Also, a save with nothing to persist now says "No changes to save." instead of reporting success. (#616, #617)

- **The Method column and the below-threshold reason disagreed on the threshold value for fields with a bare method annotation.** For fields declared `x-aws-idp-evaluation-method: FUZZY` (or `SEMANTIC` / `LEVENSHTEIN`) **without** a paired threshold, the Document detail read `Method: Fuzzy (threshold: 0.70)` next to `below threshold (0.000 < 0.5)` — same field, same run, two different numbers. Two fallbacks disagreed: Stickler's schema converter defaults such a field to `0.5` and the reason string embedded that, while `_format_evaluation_method` fell back to a hardcoded per-method table and rendered `0.70`. The fix reads the applied threshold from Stickler's model (`ComparableField` stashes the resolved value at model build time) rather than guessing, so the Method display and the scoring reason are the same number; when no threshold is available at all the suffix is omitted rather than guessed. The configured-threshold field on the dataclass is unchanged, so only the *displayed* value moves. Affected both the Section Visual Editor and the Evaluation Report's Method column. Verified live: 32 bare-`FUZZY` leaves in a `healthcare-multisection-package` run now render `0.50`.

- **The shipped `rule-validation` config preset evaluated 2 of its 14 rules.** The preset defines 7 NCCI policy classes but carried a `Document Name Regex` on only the **first** one, and rule validation evaluates only the classes whose regex matches — so the other 6 classes' 12 rules could never fire. The regex was also underscore-only (`prior_auth|pa_packet`), so the hyphenated filenames real intake systems produce matched nothing at all. All 7 classes now carry the broadened `prior[_ -]?auth|pa[_ -]?packet` pattern; the bundled sample now evaluates 14 of 14 rules. This is the `config_library` twin of the health-insurance feature preset fixed in #570. A new gate in `config_library/test_config_library.py` fails on **any** preset with multiple policy classes where a class has neither regex, so a class can no longer ship unreachable.

- **The settings cache's post-failure retry window is now an explicit deadline, instead of a timestamp back-dated past the monotonic clock's origin.** When an SSM refresh failed with an empty cache, `SettingsCache._refresh()` set `_cache_time = time.monotonic() - ttl + 30` to make the cache *look* nearly-expired. `time.monotonic()` counts from boot on Linux, so on a young container that subtraction goes negative and `_cache_time` stops meaning "a point in the past". The retry *interval* survived this (the arithmetic is relative), which is why it was never observed in Lambda; what did not survive was any **absolute** reading of the value — a unit test took exactly such a reading and so failed only on CI runners with 50–300s of uptime. Replaced with a forward-looking `_retry_after` deadline, so no stored value depends on the clock origin and the two windows are stated rather than encoded as a fake age: a full TTL when stale data is available, a short 30s window when the cache is empty. Two latent bugs fall out: the empty-cache retry window is now **capped at the TTL** (previously a sub-30s TTL got a retry window longer than itself), and `invalidate()` now clears a pending retry deadline so "refresh now" is honoured mid-deferral. The test now drives an injected fake clock and asserts on retry *behaviour*, passing identically at every simulated uptime. (#609, #610)

- **`publish` could not build the ConfBench test-set extension from source.** `sam build` failed with `PythonPipBuilder:ResolveDependencies - {pyarrow==23.0.1(wheel)}`, blocking any publish with the extension enabled in `extensions-oss.yaml`. SAM's pip builder requests exactly one platform tag — `--platform manylinux2014_aarch64` — and pyarrow's last aarch64 wheel under that tag is 20.0.0; 21.0.0 onward ship `manylinux_2_28_aarch64` only. The failure needed all of an arm64 target, an x86_64 build host, pyarrow >= 21 and no container build, which is why this extension's arm64 functions broke while the x86_64 main-stack Lambdas on the identical pin did not. Downgrading was not available: pyarrow <= 23.0.0 is affected by CVE-2026-25087 (use-after-free, HIGH). pyarrow now installs into the extension's makefile-built `SharedLayer` with the modern tag named explicitly — correct for the runtime, since python3.12 Lambda is AL2023 — and all four ingest functions already mount that layer, so they share one copy instead of embedding one each.

- **The `make test` gate silently skipped the ConfBench extension's test suite.** `feature-platform/confbench-testset/tests` (93 tests) was in neither `RUN_ROOTS` nor `QUARANTINE`, so `scripts/run_all_tests.py` **hard-failed its own registration guard** on a clean checkout — the guard that exists to stop new tests being silently skipped was itself blocking the whole gate. The root is now registered and runs (`test_planner.py` self-skips unless `huggingface_hub` and `pyarrow` are installed). The gate now covers **46 roots**, up from 45.

- **8 long-standing failures in the `idp_sdk` test suite were only a missing `sys.path` entry.** `tests/unit/test_config_operations.py` patches `idp_common.config.merge_utils.*`, which requires `idp_common` to be importable; the suite had no `conftest` entry for the in-repo package, so those 8 tests failed on any checkout and had been absorbed into the project's "known pre-existing failures" baseline. Adding the path takes `lib/idp_sdk/tests` from 279 passed / 8 failed to **292 passed / 0 failed**.

- **Addressed package vulnerabilities flagged by recent dependency scans.** Upgraded `pypdf`, `dompurify` and `js-yaml` to their patched versions. (#605, #606, #598)

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.6.4.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.6.4.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.6.4.yaml`
  

## [0.6.3]

### Added

- **Test Set - ConfBench extension: on-demand deployment of the noise-augmented FCC invoice benchmark.** New optional [Feature Platform](docs/feature-platform.md) extension that deploys the [amazon/ConfBench](https://huggingface.co/datasets/amazon/ConfBench) benchmark — the 75 verified FCC invoices of `realkie-fcc-verified`, each degraded by up to 21 Augraphy noise pipelines, 1,346 documents total — into Test Studio for confidence-calibration and OCR-robustness work. Ground truth is byte-identical to `realkie-fcc-verified`, so accuracy on any degraded variant is directly comparable to the clean baseline. Shipped as an extension rather than a fifth pre-deployed test set because the full dataset is **32.71 GB**, ~42x the combined size of the auto-deployed benchmark sets. Installing creates only the machinery (under a minute, downloads nothing); an admin then picks a size tier — clean baseline 0.02 GB / light noise 0.31 GB / representative spread 4.22 GB / full 32.71 GB — or hand-picks individual variants, with exact per-variant sizes and an approximate monthly storage cost shown before confirming. Ingest runs as a Step Functions job, so a long transfer can never fail a stack operation, and partial failures still register the test set with a per-variant failure report in S3. Note the dataset contains **21** distinct noise variants, not the "up to 18" its dataset card states — five are partial (covering 20-46 of 75 documents), which the picker labels explicitly since cross-variant accuracy comparisons over different document populations are not like-for-like. Admin-only install/ingest/delete. Contributed in PR #583 by @sujimart. See [Test Set - ConfBench](docs/extensions/confbench-testset.md) and the [ConfBench paper](docs/references.md).

- **Pipeline hooks can now modify the Document, not just observe it.** Post-step hooks were structurally read-only: the dispatcher extracted only the `halt` flag from a hook's response and discarded the rest, so a hook could rewrite the S3 objects a document pointed at, but could not relabel a section, add or drop one, correct an extracted attribute, adjust confidence alerts, or append metering. A hook may now return its modified document under `updatedDocument` (inline, or as a compressed reference it wrote itself for unbounded size) and the workflow will consume it — chained hooks at the same point compose in `order` instead of clobbering each other. Available at all six extension points, with scope set by the point: `postClassification` reaches the section Map fan-out and everything after it, while `postExtraction` runs *inside* that Map and is therefore section-scoped. New `idp_common.hooks` helpers (`load_hook_document`, `updated_document_result`) make the load → mutate → return round-trip two calls and avoid the trap of hand-building a Document (which silently drops `metering`, `errors`, `hitl_metadata`, and `processing_issues`). Guardrails: document identity (`id`/`input_key`/`input_bucket`/`output_bucket`) is immutable, a compressed reference must name an object under `compressed_documents/` in the stack's own working bucket, the `sections` list must stay well-formed, and inline documents are capped at 5 MB — a violation is refused and reported in `documentUpdateRejected` while the pre-hook document continues, never failing the workflow. Hooks should make mutations idempotent, since a retried dispatch re-invokes the hook. **Fully backward compatible**: a hook that omits `updatedDocument` passes the document through byte-identical. Set `allowDocumentUpdate: false` on a hook entry to pin it to observe-only. See [docs/feature-platform.md](docs/feature-platform.md#pipeline-hooks) and the [developer guide](docs/feature-platform-developer-guide.md). (#578)

- **Test Studio: run-level graded packet metrics.** Packet-splitting test runs now show run-level V-measure, Rand-index, clustering and ordering scores — the partial-correctness axis the exact-match counters ignore — in the Test Studio **Additional Metrics** panel. The per-document scores were already computed and stored in each doc's `evaluation/results.json`; they are now aggregated into a run-level bundle (`{mean, per_document, document_count}`) and surfaced on `TestRun.gradedPacketMetrics`. Documents that reported no graded metrics (single-section docs, classification-only runs) are omitted rather than zero-filled, and the panel is hidden entirely when no document reported them. Runs cached before this release re-aggregate asynchronously on first view and pick the panel up shortly after. See [Test Studio](docs/test-studio.md).

- **Test sets can declare which configuration version Test Studio preselects.** New optional `TestSet.configVersion` field: when a test-set record carries it, selecting that test set in **Run Test Set** preselects that configuration version. Test Studio previously inferred the configuration purely by name — it looked for a config version whose name *equals* the test set id, which is how the stack-managed benchmark sets (`fake-w2`, `docsplit`, `ocr-benchmark`, `realkie-fcc-verified`) get their pairing. That convention structurally cannot serve extension-deployed test sets, because the Feature Platform names every extension config preset `<featureId>-v<version>`, which can never equal a test set id — so those test sets always fell back to the active version and the admin had to re-pick the right configuration on every run. Resolution order is now: declared `configVersion`, then the id-equals-name convention, then the active version; a declared version that isn't in the dropdown (deleted, or outside the caller's config-version scope) falls through rather than leaving the field empty. Nothing changes for the managed benchmark sets, which don't set the field.

### Changed

- 🔒 **The CLI's PyPI distribution name is now `idp-accelerator-cli` (the `idp-cli` command is unchanged).** The name `idp-cli` on public PyPI belongs to an unrelated legitimate project — an "Internal Developer Platform CLI" with its own releases and users — so we could never publish under it, and a user who guessed `pip install idp-cli` got somebody else's tool. Renaming the *distribution* is a contained change because three other identifiers deliberately keep the old spelling: the import name (`import idp_cli`), the console command (`idp-cli`, so all documented invocations remain correct), and the `"idp-cli"` string literals used as an S3 artifact prefix, the CloudFormation `CreatedBy` tag and the Test Studio test-set name — those are persisted data, and changing them would orphan existing artifacts. `idp-accelerator-cli` is also registered on PyPI as a non-functional placeholder (`scripts/pypi-placeholders/`), so the name we now depend on cannot be taken by anyone else. **Action:** nothing is required — `make setup` picks the new name up automatically and the command you type does not change. Note that renaming a distribution does not remove the old one, so `pip list` in an existing environment will show both `idp-cli` and `idp-accelerator-cli` pointing at the same source tree; `scripts/check_first_party_deps.py` now reports the retired name and prints the `pip uninstall -y idp-cli` needed to clear it (a warning, not a failure). See [Installing First-Party Packages Safely](docs/dependency-confusion.md).

- **Evaluation: Stickler is now the single scoring source of truth (package reorganization).** End-to-end overhaul of the evaluation package's use of `stickler-eval==0.5.0`. Verdicts, counts, and derived metrics all come from Stickler's `confusion_matrix` directly, and new Stickler capabilities are surfaced for the first time: `DateComparator`, graded packet metrics (`final_score`, `clustering_score`, `v_measure`, `rand_index`, `avg_ordering_score`), a confidence accumulator with index collapsing (`LineItems[N].Rate → LineItems.Rate`), weight-aware document-level rollup, and `clip-under-threshold` / `aggregate` per-field pass-throughs. `EvaluationMethod` gained `LEVENSHTEIN`. Internally, a new `stickler_backend/` subpackage is the only code path allowed to `import stickler`, a new `contract.py` formalizes the cross-Lambda `results.json` payload with a `STICKLER_RESULT_VERSION` stamp, and ~1,700 lines that duplicated Stickler functionality are deleted (the local `DocSplitClassificationMetrics` fork, `metrics.py`, the legacy fields-config mapper path, `ConfidenceMetricsCalculator`, non-null default injection), dropping the `munkres` dependency and relaxing the numpy pin to `>=1.26,<3`. A new golden-fixture regression suite gates every future scoring-behavior change against real unmocked Stickler. Docs updated across both tiers ([docs/evaluation.md](docs/evaluation.md), `evaluation-enhanced-reporting.md`, `test-studio.md`, [evaluation/README.md](lib/idp_common_pkg/idp_common/evaluation/README.md), `step7_evaluation.ipynb`). Live-validated on 50 single-section and 50 multi-section documents.

- **Sample: Health Insurance Review v0.1.3 — Claims dashboard upgraded to a full Cloudscape collection table, claims can now be deleted, and the sample docs point at a working sample document.** The Processed Claims list now matches the Document List UX: multi-select with a **Delete** action (confirmation modal; new `DELETE /claims/{docId}` feature-API route, Admin/Author only — deleting removes only the dashboard record, never the document or its rule-validation outputs), text filtering, sortable + resizable columns, pagination, and table preferences. The empty-state instructions and [docs](docs/extensions/sample-health-insurance-review.md) now point at `samples/rule-validation/medicare_respiratory_pa_packet.pdf` — whose content actually matches the bundled NCCI policy rules and produces real Pass/Fail results (verified: 9 Pass / 5 Fail) — and the two `Prior-Auth-*.pdf` samples are removed: their content (an allergy/immunotherapy history) matched none of the NCCI surgical-coding rules, so every rule returned "Information Not Found" and the claim always landed as Insufficient documentation, which read as a bug. The sample set is now just the policy manual (for Rules Discovery) + one working claim packet.

- **Removed the "Files" column from the document Version History panel.** It showed `FileCount` — the number of output S3 objects the run's manifest pinned (`sections/*/result.json`, page text/images, reports) — an internal storage-plumbing detail that reads as "how many files were in my document" (that's the adjacent **Pages** column) and offers no action. The attribute is still recorded on each run record and returned by `listDocumentVersions`/`getDocumentVersion`, and `idp-cli list-versions` still displays it. See [docs/document-versions.md](docs/document-versions.md).

### Fixed

- 🔒 **Dependency confusion on first-party packages: `make setup` could install a third-party `idp-sdk` from public PyPI instead of the one in `lib/`.** The first-party packages depend on their siblings by bare name (`lib/idp_cli_pkg` requires `"idp-sdk"`, `lib/idp_sdk` requires `"idp_common"`), and neither is published to PyPI — but both names **are** registered there by an unrelated third party. Because setup installed the packages one `pip install` at a time and installed `idp_cli_pkg` *before* `idp_sdk`, pip resolved the not-yet-installed sibling from the public index and silently installed the squatted package into the environment that holds AWS deployment credentials. The observed payloads were inert (they print a banner; no install hooks, no network), but the name owner can publish a new version at any time. Six fixes: (1) `make setup` / `make setup-venv` now install all first-party packages in a **single** pip invocation, so pip satisfies the sibling names from the local checkout; (2) the same consolidation in CI, which additionally (3) runs a new tripwire, `scripts/check_first_party_deps.py`, that uses [PEP 610](https://peps.python.org/pep-0610/) `direct_url.json` (written for local/VCS installs, absent for index installs) to fail the build if any installed first-party package came from an index; (4) the two still-unclaimed names, `idp-feature-sdk` and `idp-mcp-connector`, are registered defensively on PyPI as deliberately non-functional placeholders that raise on import (`scripts/pypi-placeholders/`) so nobody else can take them; (5) every install instruction in the docs and in runtime error messages now uses a local path (`pip install -e "lib/idp_common_pkg[...]"`) — previously several, including the `idp-cli` error text, told users to `pip install idp-sdk`, i.e. to install the squat; and (6) `cli.py` no longer masks the failure behind an unrelated `AttributeError: 'NoneType' object has no attribute 'group'`. **Action:** if you have an existing dev environment, run `python scripts/check_first_party_deps.py`; if it fails, `pip uninstall -y idp_common idp-sdk idp-cli idp_feature_sdk idp_mcp_connector && make setup`. See the new [Dependency Confusion](docs/dependency-confusion.md) guide.

- 🔒 **S3 buckets created by the CLIs and by the sample notebooks had no `EnforceSSLOnly` bucket policy, so they accepted plain-HTTP requests.** All 13 buckets in `template.yaml` (plus the SDLC pipeline's `ArtifactBucket`) carry an `AWS::S3::BucketPolicy` denying `s3:*` when `aws:SecureTransport` is false — but the buckets created *imperatively* outside CloudFormation never got the equivalent. Three code paths are fixed via a new shared `idp_sdk._core.s3_security` helper: the `idp-cli publish` artifacts bucket, the `idp-cli deploy` config-staging bucket, and `idp-feature-cli`'s `ensure_artifacts_bucket`. Two more gaps closed alongside: `scripts/sdlc/cfn/s3-sourcecode.yml`'s `InitialInstallBucket` gains a bucket policy, and the 11 sample notebooks' `ensure_bucket_exists` helper now applies the policy to buckets it creates. The statement is applied **additively** — existing statements are preserved, a stale `EnforceSSLOnly` is replaced rather than duplicated so re-running is idempotent, ARNs use the region's real partition (`arn:aws-us-gov:` in GovCloud), and both forms the IAM grammar allows for `Statement` (a single object as well as an array) are handled. Because this only ever *tightens* access it is also applied to pre-existing buckets, best-effort there (a `PutBucketPolicy` denial warns and continues rather than failing the publish, since the operator may own that bucket's policy). The `idp-cli deploy` config-staging bucket is hardened on **reuse** as well as creation, so buckets left unhardened by an older `idp-cli` don't stay that way. **Note:** running the sample notebooks now requires `s3:PutBucketPolicy` on the buckets they create. See [lib/idp_sdk/README.md](lib/idp_sdk/README.md#buckets-the-cli-creates) and [lib/idp_feature_sdk/README.md](lib/idp_feature_sdk/README.md#artifacts-bucket-security). (#581)

- 🔒 **CI workflow supply-chain hardening.** Three classes of weakness in the GitHub Actions workflows, all fixed rather than suppressed: four third-party actions in `deploy-docs.yml` were referenced by mutable tag instead of commit SHA; `developer-tests.yml` expanded the fork-controllable `github.base_ref` directly inside a `run:` script (a shell-injection vector, now passed via `env:` and expanded quoted); and the same workflow installed `uv` by piping `astral.sh/uv/install.sh` into a shell, replaced with the SHA-pinned `astral-sh/setup-uv` action.

- **Developer Tests CI job no longer fails with `npm: not found` when NodeSource is unreachable.** The workflow installed Node 22 by piping NodeSource's installer into bash. That pipeline reports bash's exit status, not curl's, so when NodeSource returned HTTP 403 the failure was swallowed: the apt repo was never registered, the following `apt-get install -y nodejs` quietly installed Debian bookworm's own **nodejs 18** — which ships no `npm` — and the job died several steps later with a bare `npm: not found`, giving no hint that a download had failed. Node is now installed with the first-party SHA-pinned `actions/setup-node` action, which fails loudly and immediately on a download problem. See [.github/workflows/developer-tests.yml](.github/workflows/developer-tests.yml). (#575)

- **Document details page rendered several surfaces against the stack's *live* config instead of the config version the document was actually processed with.** Reported as: entering **Edit Mode** and opening the class/type `<Select>` in the Sections table listed the accelerator's shipped defaults (`W2`, `Payslip`, `BankStatement`, …) regardless of which config version the document had been classified against — so a user with a custom config saw the wrong vocabulary and, worse, could reassign a section to a class that did not exist in the document's own configuration. The same class of bug affected two adjacent surfaces on the same page: the **header Confidence Alerts** badge computed its count against the stack's current `hitl.confidence_threshold` (so it could disagree with the per-section alert counts inside the Sections panel), and the **Processing Flow** diagram greyed out step nodes according to the stack's current `summarization.enabled` / `extraction.confidence.enabled` / `assessment.enabled` / `evaluation.enabled` toggles, misrepresenting what was actually enabled when a historical document ran. All three now consume the versioned config that `DocumentPanel` already loads from the document's own `ConfigVersion`. The Edit Mode class `<Select>` also gains `expandToViewport` so the popup is not clipped by the small table cell, `filteringType="auto"`, and each option now carries its class `description` from the config schema. (#586)

- **Per-field confidence thresholds in `$defs` (array items via `$ref`) now resolve correctly.** When a class schema declared array item fields with per-field `x-aws-idp-confidence-threshold` inside `$defs` (referenced via `$ref`), the individual thresholds were ignored — every sub-field received the flat `hitl.confidence_threshold` (often 0.0), so fields that should have triggered HITL review never did. Both the pipeline assessment and BDA processing paths now resolve `$ref` → `$defs` to apply the declared per-field threshold, so fields below their declared threshold correctly appear as low-confidence and trigger Review Pending status. The class-schema lookup that both paths depend on is now a single shared helper (`idp_common.assessment.threshold_resolver.find_class_schema`) that tolerates a non-string `x-aws-idp-document-type` — legacy→schema config migration sets that key to the boolean `True` as a marker, which previously raised `AttributeError` and, on the BDA HITL path, failed the segment outright. One documented limitation: thresholds declared *below* the first array level (e.g. `w2_copies[].address.zip`) are honored by the HITL alert path but fall back to `hitl.confidence_threshold` in the `result.json`/pipeline enrichment path; see [docs/extraction-and-confidence.md](docs/extraction-and-confidence.md#thresholds-inside-lists-arrays).

- **Evaluation: silent scoring bugs in the Stickler integration.** Two correctness fixes in the production evaluation path: (1) `NUMERIC_EXACT` tolerance and the Hungarian `match_threshold` never actually reached Stickler — the tolerance gate was a no-op because `NumericComparator.compare` is binary, and `match_threshold` was written to the array property instead of the items schema, so Stickler's element-class builder kept its default of 0.7; (2) per-document FAR/FDR disagreed with the run-level dashboard, because the run-level formulas derived both rates from the combined `fp` count. Since `fp == fa + fd`, that folded false discoveries into the false-alarm rate and vice versa, so the same document reported different error rates in the per-document view and the Test Studio dashboard. Both sides now derive FAR from Stickler's `fa` and FDR from `fd`.

- **Changing a UI-affecting CloudFormation parameter on an existing stack had no effect, because the Web UI was never rebuilt.** Reported as: adding `AllowedSignUpEmailDomain` to a running stack correctly flipped the backend — self-service signup really was permitted — yet the Cognito login screen still hid the **Sign Up** tab, which a *new* stack with the same parameter offers. The cause is a build-time/runtime split: the UI reads these settings as `import.meta.env.VITE_*`, which Vite substitutes textually at `npm run build` and freezes into the deployed JS bundle. On a parameter change CloudFormation dutifully updated the CodeBuild project's env var — but mutating a project *definition* does not run a build, and the `CodeBuildRun` custom resource that does had no property diff, so CloudFormation skipped it entirely and the stale bundle stayed deployed. `CodeBuildRun` now carries a `UIBuildInputs` property joining every parameter Vite bakes in — `AllowedSignUpEmailDomain`, `ExternalIdPType`, `ExternalIdPName`, `ExternalIdPAutoLogin`, `EnableQuickStartWidget`, `WebUIHosting`, `CustomDomainUrl` — so a parameter-only update rebuilds the UI. **The same latent bug affected all seven**, not just signup: the external-IdP sign-in button, IdP auto-login, the Quick Start widget, and the asset base path / OAuth redirect origin all silently failed to take effect on an update-only change. Deliberately *not* listed are values the UI resolves at runtime from the SSM settings parameter (`ConsoleTitle`, `DefaultFeatureId`, `AllowedSignUpEmailDomains`) — those already apply on the next page load, and forcing a ~30-minute UI rebuild for them would be a regression. A new static test derives the baked-parameter set from the template itself and asserts every member is a rebuild trigger. See [docs/web-ui.md](docs/web-ui.md#parameters-baked-into-the-ui-at-build-time) for the manual-rebuild recovery command. (#580)

- 🔒 **Dependabot security bumps to the multi-doc discovery container never reached the built image.** The nested stack's CodeBuild buildspec heredoc'd its *own* inline copies of `requirements.txt` and the `Dockerfile` into the build directory, so the checked-in `nested/multi-doc-discovery/{requirements.txt,Dockerfile}` were dead files that nothing consumed. The two silently forked: Dependabot bumped the on-disk `requirements.txt` to `Pillow==12.3.0` (PR #530) while the image went on installing the inline `Pillow==12.1.1` — a merged CVE patch that never shipped. Compounding it, **two separate rebuild gates also ignored those files**: neither was listed in the component's smart-rebuild dependencies, and — the load-bearing one — the `BuildHash` property that is the *sole* trigger for re-running the container build hashed only `src/lambda/multi_doc_discovery`. A bump therefore left `BuildHash` byte-identical, so on an in-place stack update CloudFormation never re-invoked the build and the ECR image kept the vulnerable dependency. (A fresh stack create always builds, which is why this gate was invisible to end-to-end create testing.) The buildspec now builds `-f nested/multi-doc-discovery/Dockerfile` from the source zip (which `publish.py` now includes, failing the publish loudly if either input is missing), both files feed `BuildHash` **and** the component dependencies, and tests assert both that the buildspec contains no inline dependency pins and that `BuildHash` actually moves when either build input changes. See [nested/multi-doc-discovery/template.yaml](nested/multi-doc-discovery/template.yaml).

- **A transient PyPI connection drop during the container build no longer fails the whole stack deploy.** A mid-stream `BrokenPipeError` while pulling a large wheel (numpy is ~18 MB) surfaces as a hard `pip ... exit 2`, which cascaded unrecoverably: `docker build` failed → `docker push` failed → CodeBuild FAILED → the `DockerBuildRun` custom resource reported FAILED → the nested stack rolled back → the entire deploy failed, ~26 minutes in, on a fault unrelated to the change under test. The `pip install` steps now use `--retries 10 --timeout 60` inside a bounded 3-attempt retry loop (pip's own retries do not cover stream-level faults), `docker push` retries up to 3 times, and the `DockerBuildRun` custom resource retries a FAILED/FAULT build once — within its Lambda budget, always reserving time to answer CloudFormation, and never retrying a deliberately STOPPED or TIMED_OUT build. Its failure message now names the build and project so a reader knows which log stream holds the real cause. (#576)

- **A rollback could wedge the stack in `ROLLBACK_FAILED`, requiring manual intervention.** S3 permits only one conditional bucket-config operation against a bucket at a time, but CloudFormation deleted `TestSetBucketPolicy`, `TestSetBucketAutoDelete` and `TestSetBucketNotificationConfiguration` in the same instant, so `PutBucketNotificationConfiguration` returned `OperationAborted` ("A conflicting conditional operation is currently in progress"). The inline handler had no retry and failed hard, turning a recoverable rollback into `ROLLBACK_FAILED` — even though the identical call succeeded three minutes later. Three fixes: the handler retries transient S3 codes on a bounded backoff ladder, a **Delete** now always reports success so notification cleanup can never block a teardown (the bucket is `Retain` anyway), and `DependsOn: TestSetBucketPolicy` serializes the two conditional writes to remove the race at its source. (#576)

- **CI deployment summaries now name the real root cause instead of dead-ending at "CodeBuild failed".** When a nested stack failed because its container build failed, the only evidence in CloudFormation was `CodeBuild failed with status: FAILED` — the actual error (a pip network fault, a Docker layer error, an ECR auth failure) lived solely in that build's own log stream, which no summary ever read, so every such failure produced an unactionable report. The harness now resolves the failing CodeBuild project from the nested stack that reported it, finds its most recent build in a **terminal failure** state, and attaches that build's failing phase, phase error, log tail, and console URL to the summary (snapshotted before teardown). Two related corrections: the recovery command is now derived from the stack's real status — a CREATE rollback lands in `ROLLBACK_FAILED`, where the previously-suggested `continue-update-rollback` is invalid — and the primary failure that *caused* a rollback is reported separately from any secondary failure that merely *blocked* it. See [scripts/sdlc/codebuild_deployment.py](scripts/sdlc/codebuild_deployment.py). (#576)

- **`deleteTests` reported success while orphaning every underlying document.** The resolver deletes test-run metadata and baselines, then fans out to the `deleteDocument` Lambda to remove the documents themselves — but that fan-out payload carried only `arguments`, no `identity`, and `deleteDocument` enforces Admin/Author server-side. Every one of those invokes was rejected with `PermissionError`; because the fan-out uses `InvocationType='Event'`, the rejection was invisible to the caller, which logged "Invoked document delete for N total documents" and returned true. Documents were therefore left behind in the tracking table and S3 on every delete, via the Web UI as much as any programmatic caller. The already-authorized caller identity is now forwarded, so the downstream check stays meaningful (it re-verifies the same membership) rather than being bypassed.

- **Deleting a test set with more than 1,000 objects silently orphaned the rest.** `deleteTestSets` listed the test set's S3 objects with a single unpaginated `list_objects_v2` and passed the result to one `delete_objects` call. Both APIs cap at 1,000 keys, so for any larger test set the DynamoDB record was removed — the set vanished from Test Studio — while every object past the first page stayed in the bucket: invisible to the UI, still counting against storage, and still subject to nothing but the bucket's retention rule. Pre-deployed sets already exceeded the cap (Fake-W2-Tax-Forms is 2,000 documents, ~4,000 objects counting baselines), so this was reachable without any unusual setup. Both loops are now paginated, with a guard against a truncated response that carries no continuation token.

- **Test Studio showed "N/K completed" where N > K when an S3 test-set folder had drifted past the test set's declared `fileCount`.** Reported as `85/10 completed` on `realkie-fcc-verified` and `2005/5 completed` on `fake-w2`. The runner wrote `FilesCount = test_set.fileCount` (10, 5) into the run's metadata, but the file copier listed *everything* under the test set's `input/` prefix — 85 and 2005 objects respectively, because extra files had been dropped into those folders outside the test-set flow — and stamped a document record for each. The status resolver then counted every one as completed against the unchanged denominator. The runner now always sends `filesToProcess` in the copier's payload, and the copier caps the S3 listing at that count after a deterministic sort (so repeat runs pick the same subset) and warns loudly when the folder exceeds the declared count.

- **Historical document versions no longer report "Low Confidence Fields: 0" for every section.** Viewing a past version showed a zero low-confidence count (and an empty section Status) even when that run had low-confidence fields, because the per-section quality data was dropped at all three layers: `create_document_run` never snapshotted `ConfidenceThresholdAlerts`/`ProcessingIssues` into the run record, the versions resolver omitted them, and the `GetDocumentVersion` query didn't select them. All three now carry the fields, so a historical view renders the same counts and section status the live document does. **Note:** the snapshot is written at run-completion time, so this applies to runs recorded *after* upgrading — versions already in the table have no stored alerts and will continue to show 0. (#573)

- **Rule Validation report/tab missing from the document detail page.** The document detail UI renders the "View Rule Validation Summary" button and the Rule Validation tab only when the `getDocument` response carries the flat `RuleValidationResultUri` scalar, but the write path stored only the nested `RuleValidationResult` object — the flat scalar was never persisted after document writes moved to `dynamodb/service.py`. It is restored in both the `update_document` expression and the `create_document_run` snapshot item, so a completed rule-validation run surfaces its report in the UI. (#572)

- **MCP `get_results` can now fetch a single document's results, and one broken tool no longer disables the whole MCP tool suite.** Two fixes for external MCP consumers (e.g. downstream gateways fed by post-processing hook events, which carry a document reference but no batch id): (1) `get_results` accepts a new `document_id` argument — the document's S3 object key or its `s3://` output-prefix URI — as an alternative to `batch_id`, backed by a new `idp_sdk` `client.batch.get_document_results()`; the gateway tool schema no longer hard-requires `batch_id`, and the schema is now re-synced onto existing gateway targets during stack updates (previously it was only sent at target creation, so schema changes never reached deployed gateways). (2) The MCP handler imported the analytics agent stack (`strands`) at module load, so a missing/incomplete agents Lambda layer failed the import of the whole handler — every tool died with `Runtime.ImportModuleError`. Tool modules are now imported lazily per call: batch tools work regardless, and `search` returns a structured error naming the missing dependency. See [docs/mcp-server.md](docs/mcp-server.md). (#567)

- **Extension "Update available" badge no longer offers a downgrade.** `listInstalledFeatures` computed `updateAvailable` as `latestVersion != installedVersion`, so any difference lit the badge — including a catalog *behind* the installed version. An admin running an extension at v0.1.1 was told "Version v0.1.0 is available". That state is routine rather than exceptional: `idp-feature-cli deploy --from-code` (the documented per-extension dev loop) installs a newer extension immediately, while `catalog.json` only refreshes on a host stack create/update, so the catalog is legitimately older for the whole window between the two. It now compares parsed SemVer and requires strictly newer — numerically (`0.1.10` > `0.1.9`, which string comparison gets backwards) and with SemVer §11 prerelease precedence (`1.0.0` is an update over `1.0.0-rc1`, not the reverse). Non-SemVer version strings fall back to the previous inequality check and log a warning, so an unusual scheme over-reports rather than hiding a real update.

- **Extension UI bundles no longer stay stale for up to a year after a same-version republish.** All feature ui-deployers (the template plus every bundled extension) copied `ui-bundle.js` into the Web UI bucket with `Cache-Control: max-age=31536000,immutable`. The destination key is version-addressed, so version bumps bust caches — but same-version republishes do happen (hotfixes re-uploading an existing version), and the immutable header pinned the old bundle in browsers until the version changed. They now use `max-age=300`, matching the CloudFront distribution's own TTL, so an update propagates within minutes. (#568)

- **Test Set Generator — generating into an existing test set no longer overwrites its documents or clobbers its metadata (v0.1.2).** Several fixes to the [Test Set Generator extension](docs/extensions/idp-data-generator.md): (1) each generation run now prefixes its uploaded filenames with a per-run token, so appending to an existing test set **adds** documents instead of overwriting the prior run's identically-named `doc_0001.pdf`…; (2) the generate modal separates the destination (**create a new test set** vs **add to an existing one**) from the config version, and a collision guard — client-side and re-checked in the feature API — blocks "create new" against a name that already exists (including a set still `GENERATING`); (3) the host test-set record is written via a non-clobbering upsert that preserves an existing set's name/description/`createdAt` and populates `InitialEventTime` so the record stays visible to the Test Studio list; (4) a failed **append** run restores the set to `COMPLETED` (with its true document count) rather than marking the whole set `FAILED`; (5) both generate routes now require the caller to be in the `Admin` or `Author` Cognito group; and (6) reinstalling the extension after a delete no longer fails with a `LogGroup` "already exists" error — the two custom-resource Lambdas that run during the Delete phase could silently re-create their own CloudFormation-managed log groups outside CloudFormation, and their IAM is now scoped so a late log write is dropped instead. See [docs/extensions/idp-data-generator.md](docs/extensions/idp-data-generator.md).

- **Sample: Health Insurance Review — prior-auth documents produced no rule-validation output and never appeared in the Claims dashboard.** Two compounding defects. (1) The bundled config preset's policy classes matched the document *filename* with `x-aws-idp-document-name-regex: ...(prior_auth|pa_packet)...` — an underscore — but the sample documents it ships with use a hyphen, so classification returned `NO_POLICY_MATCH`, rule validation was skipped, and zero rules were evaluated. The regex now accepts any of `-`, `_`, or space across all seven policy classes. (2) The unified workflow's rule-validation skip paths (`SetSkippedRuleValidationResult` for no-policy-match, `SetEmptyRuleValidationResult` for rule-validation-disabled) jumped straight to summarization, **bypassing the `PostRuleValidationHook` dispatch** — so a document that skipped rule validation never fired the feature's `postRuleValidation` hook and thus never recorded a claim, even to flag it as needing documentation. Both skip states now route through `PostRuleValidationHook`, and the feature's hook records a `NO_POLICY_MATCH` run as `INSUFFICIENT_DOCUMENTATION`. See [patterns/unified/statemachine/workflow.asl.json](patterns/unified/statemachine/workflow.asl.json). (#571)

- **Sample: Health Insurance Review — Rules Discovery tab failed with "No GraphQL endpoint configured in `Amplify.configure()`".** The feature's Rules Discovery flow still called `aws-amplify/api`'s `generateClient().graphql()`, which stopped working when the host replaced AppSync with the REST dispatcher (Amplify now carries no GraphQL endpoint — it is configured for Cognito auth only). The feature now uses the host's REST-backed, GraphQL-shaped client exposed at `window.IdpFeatureHost.generateClient`, so uploadDiscoveryDocument / listDiscoveryJobs / getConfigVersion go through the same transport as the host UI. (#569)

- **Pinned `ruff` for the format gate.** `pyproject.toml` now pins `ruff<0.16`: 0.16.0 began reformatting Python code blocks embedded in Markdown, which failed `ruff format --check` on unchanged `.md` files repo-wide, unrelated to any diff.

- **Addressed package vulnerabilities flagged by recent dependency scans.** Upgraded affected direct and transitive dependencies across the web UI (`src/ui`), the Python library (`lib/idp_common_pkg`), and the feature-platform UIs to their patched versions.

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.6.3.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.6.3.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.6.3.yaml`
  

### Added

- **Z3 Dual-Engine Rule Validation** — Rules can now be individually routed to either the LLM (semantic) or Z3 (formal/deterministic) validation engine via the `x-aws-idp-validation-engine` field on each rule property in `policy_classes`. The Z3 engine translates natural-language rules to SMT-LIB constraints, extracts parameter values (path-based or LLM-assisted), and validates with the Z3 theorem prover. Z3 failures automatically fall back to the LLM engine. A new "Validation Engine" dropdown in the Schema Builder UI lets users select the engine per rule. Requires `z3-solver` (optional dependency, loaded lazily only when Z3 rules are encountered). See [`docs/rule-validation-z3.md`](docs/rule-validation-z3.md) and the [demo notebook](notebooks/examples/dual-engine-rule-validation.ipynb).

## [0.6.2]

### Added

- **Generic `preprocessing` pipeline-hook point.** A new standalone extension point that runs *first* in the document-processing workflow — before the BDA/pipeline routing — so it fires in both processing modes and even when OCR is disabled, operating on the source document itself. Unlike the post-step `postHook` lists, it is a single flat hook in its own top-level `preprocessing` config section (`arn` + generic key/value `args`, no feature-specific fields), editable in the View/Edit Configuration UI and reusable for any preprocessing job. A hook may return `halt: true` to end the execution (e.g. after spawning a replacement document), and `onError: fail` is terminal — a failed hook stops the execution rather than falling through to processing the un-preprocessed original. While the hook runs, the document's visible status shows **`PREPROCESSING`** (a new `Status` member, abortable like other in-flight statuses) instead of the generic `RUNNING`. Fully backward-compatible: a no-op when no hook is registered. New host export `<MainStackName>-InputBucketName`. See [Feature Platform → Pipeline hooks](docs/feature-platform.md#pipeline-hooks).

- **PII Anonymization extension.** A bundled [Extension Feature](docs/extensions/pii-anonymizer.md) — the reference consumer of the new `preprocessing` hook point — that detects and redacts PII from source documents **before** the classification/extraction models see them, so the document-processing pipeline (classification, extraction, assessment, summarization — including its prompts, logs, and stored results) operates only on de-identified content. (The redaction step itself uses a Bedrock model to *detect* the PII, so PII does transit that single detection call.) The hook writes a de-identified copy of each document back to the Input bucket beside the original with a `(REDACTED)` marker in its name (with a re-entrancy guard against redaction loops), tagged to re-process under a **companion** config version; two modes — *redact copy and stop* (the original is deleted so only the redacted copy remains) and *redact copy and continue* (original + redacted as separate documents, scoped by `allowedConfigVersions` RBAC). PDFs are redacted PDF-in/PDF-out (image path, flattened, no leaked text layer). A **Config Pairing** wizard clones an admin's existing config version into the matched pair via host config APIs; a **Redaction Report** tab shows a metadata-only audit (no PII stored) and — opt-in, stored in a feature-owned encrypted DynamoDB table and RBAC-gated to users with access to the original's config version — the original→synthetic PII mapping. Flagged **experimental**. Reuses the document detection/redaction library from AWS Labs [pii-anonymizer](https://github.com/awslabs/pii-anonymizer) (Apache-2.0), vendored under the feature with a re-sync skill. See [docs/extensions/pii-anonymizer.md](docs/extensions/pii-anonymizer.md).

- **Test Set Generator extension — generate labeled synthetic test sets from Test Studio.** A new installable [Extension Feature](docs/extensions/idp-data-generator.md) (feature id `idp-data-generator`, AgentCore Runtime) adds a **Generate Synthetic Data** button to **Test Studio → Test Sets**: describe a document type in plain language (or pick an existing configuration version), optionally add a scenario theme (with a Bedrock-backed **Suggest** helper) and a quality setting, review the live cost/time estimate, and a background job lands the labeled synthetic documents as a new test set. Generated documents can be previewed in-place, and a **Generate test set** button on **View/Edit Configuration → Document Schema** deep-links into the pre-filled modal. Generation is powered by the open-source [SEED](https://github.com/awslabs/synthetically_engineered_evaluation_data) package (`seed-data`), which the Quick Start / `idp-cli bootstrap` generator also uses (optional `idp_common[synthesis-generator]` extra — schema authoring, catalog matching, and config/test-set creation all work without it). A runtime watchdog fails a wedged job instead of leaving it stuck IN_PROGRESS. See [docs/extensions/idp-data-generator.md](docs/extensions/idp-data-generator.md).

- **Claude Opus 5 model support.** Anthropic's Claude Opus 5 (`anthropic.claude-opus-5`, launched on Bedrock 2026-07-23) is now selectable everywhere Opus 4.8 is: all service model dropdowns (classification, extraction incl. per-class overrides, assessment, summarization, confidence, evaluation, chat), the `us.`/`eu.`/`global.` geo cross-region inference profiles, and the `:1m` long-context variants. Pricing matches Opus 4.8 ($5/$25 per MTok standard; $10/$37.50 above 200K input on `:1m`); prompt caching and reasoning effort (`low`–`max`) are supported, and sampling parameters (temperature/top_p/top_k) are stripped as for Opus 4.7/4.8. Notes: Opus 5 has a 1M-token context window natively, but the base ID keeps the 200K auto-sizing budget (pick the `:1m` variant to opt into the full window and its long-context pricing); thinking is on by default (runs within `max_tokens`); no `jp.` geo profile (unlike Opus 4.8) and not yet available in GovCloud. US/EU region filtering maps `us.` ↔ `eu.` variants directly.

- **Test Set document browser with ground-truth visual editor.** Click a COMPLETED test set's name in Test Studio (or select it and click **Browse Documents**) to open `/test-studio/sets/<id>`: a paginated table of the set's documents with lazy client-rendered first-page **thumbnails** (pdfjs ranged fetch — large PDFs only download the chunks needed for page 1). Each document name links to a per-document detail page (`/test-studio/sets/<id>/doc/<file>`, mirroring the Document List → Document Details structure) with two views: an inline **View Source Document** viewer, and an **Edit Ground Truth** visual editor showing the document's page images (rendered in-browser from the source PDF/image) beside an editable form over the baseline's `inference_result`, with a raw JSON tab, a multi-section selector for packet-splitting sets (page images scoped to each section's `split_document.page_indices`), and bounding-box field highlighting when the baseline carries `explainability_info` geometry. Saves write back to the test set's `baseline/.../result.json` with an `_editHistory` provenance entry (Admin/Author; other roles read-only). Backed by a new Admin/Author `getTestSetDocuments` API (paginated, optional exact-match `objectKey` filter); the TestSetBucket CORS now permits GET/HEAD + ranged reads for the browser's presigned fetches, and the file-contents/upload resolvers gained TestSetBucket read/write grants. See [Test Studio](docs/test-studio.md#browsing-test-set-documents-and-ground-truth) and [Creating Custom Test Sets](docs/creating-custom-test-sets.md).

- **Central input-shape validation on the UI API.** The `POST /op/{field}` dispatcher now validates every request's `arguments` against a build-time spec generated from `schema.graphql` — rejecting unknown arguments, missing required (non-null) arguments, wrong scalar types, and out-of-set enum values with HTTP 400, before the request reaches a resolver. This re-establishes the boundary input-shape gate that AppSync provided and the REST migration lost, closing a type-confusion / unexpected-argument defense-in-depth gap. It is intentionally **stricter than AppSync on input coercion** (AppSync coerced a scalar to a one-element list, an int to an `ID` string, and accepted bare-scalar `AWSJSON`; this validator rejects those) — safe for the current Web UI, which sends list args as arrays, IDs as strings, and `AWSJSON` as `JSON.stringify` objects (verified across all UI operations). The validator is stdlib-only (no runtime GraphQL dependency), conservative (type-only checks, shallow input-object validation), and fails open on its own internal errors so a validator bug can never take down the API. A drift guard keeps the spec in sync with the schema in CI. Threat model updated (AUTH.T12); see [AppSync→REST migration §4](docs/migration-appsync-to-rest.md).

- **Dynamic security testing (DAST) in the integration pipeline.** An authenticated [OWASP ZAP](https://www.zaproxy.org/) scan of the deployed UI REST API now runs on every integration build as the `zapdast` deployment-variant probe — on its own throwaway stack, fully concurrent with the existing suite (≈zero added wall-clock). It adds the class of coverage neither the SRT static scan nor the RBAC authorization harness provides: injection (XSS/SQLi), missing security headers (CSP/HSTS/X-Frame-Options), cookie/TLS flags, and information disclosure. Because the API is a single Cognito-gated `POST /op/{field}` route with no OpenAPI spec, the scan is seeded from `scripts/api_rbac_expectations.yaml` (the shared op source-of-truth) and authenticated with a Cognito token via the shared `scripts/rbac_common.py`. WARN-only for now (full HTML/JSON report uploaded to S3 and pulled into GitLab artifacts; rule actions tunable in `scripts/sdlc/zap-rules.conf`), with a documented path to promote high-confidence rules to hard-fail. Passive baseline runs every build; the active scan is opt-in via `IDP_ZAP_ACTIVE=true`. Requires `PrivilegedMode` on the `app-sdlc` CodeBuild project for Docker. See [CI test coverage](scripts/sdlc/docs/CI_TEST_COVERAGE.md#zap-dast-probe-dynamic-security-testing).

- **Auditable security test results.** A new top-level `security/` directory collects the threat model (moved from `threat-modeling/`) and, under `security/test-results/<version>/`, curated Markdown snapshots of the four security tests — SRT (SAST/deps), ZAP DAST, and RBAC static + dynamic — each with a `MANIFEST.md` tying results to a version, git SHA, and date. `security/README.md` documents each test's coverage, goals, and how to run it (with threat-model cross-references). A single command — `make security-results STACK_NAME=<stack>` (or just `make security-results` for the offline SRT + RBAC-static tests) — runs the tests and regenerates the snapshot; `scripts/security/curate_results.py` generates the Markdown and is **public-safe by construction**: it re-emits only publish-safe fields, redacts environment identifiers (account IDs, Cognito pool IDs, API hostnames, request IDs, local paths, AWS keys/secrets), and reconciles SRT findings to the CI view. Each result file leads with a summary and enumerates the tests that executed (ZAP per-rule outcomes, the RBAC op×role matrix, the static S1–S5 checks, SRT analyzers). See the [curate-security-results skill](.claude/skills/curate-security-results.md) and [CI test coverage](scripts/sdlc/docs/CI_TEST_COVERAGE.md#security-tests-coverage--auditable-results).

### Changed

- **Catalog features can now opt out of the Extensions nav until installed (`showInNav`), and the bundled samples do.** Previously every catalog feature got a side-nav entry — so the two bundled samples (*Sample: Document Status (feature add-on)* and *Sample: Health Insurance Review*) appeared with Install badges even on stacks where they were never deployed. A new optional `showInNav` flag (default `true`) in `feature.yaml` (OSS) / `extensions-marketplace.yaml` (marketplace) controls whether a not-yet-installed catalog feature gets its own nav entry; the two samples set it to `false`. A new **Browse catalog** nav link — pinned at the top of the Extensions section, italicized and separated from the extension links by a divider so it reads as the catalog entry point rather than another extension — opens a catalog browser page (`/features`) listing every extension — installed and available, including nav-hidden ones — with status badges and links to each feature's install/update page. Installed features always appear in the nav regardless of the flag. See [docs/feature-platform.md](docs/feature-platform.md).

- **Renamed the `EnableHeadless` CloudFormation parameter to `EnableJobsApi`.** The old name conflated this parameter with "headless mode" — but it is an *additive* switch that stands up the Jobs REST API (Private API Gateway + `/jobs` endpoints + supporting Lambdas + a machine-to-machine Cognito OAuth client) **in addition to** the Web UI; it does not remove the UI. (That is the separate `idp-cli deploy --headless` template transform, which is unchanged.) The parameter, its metadata (ParameterGroups/ParameterLabels), the `JobsApiRequiresVPC` rule, and all docs now say "Jobs API". **Upgrade note (breaking parameter rename):** existing stacks deployed with `EnableHeadless` must supply `EnableJobsApi` (with the same `true`/`false` value) on their next stack update — the old parameter name is no longer recognized. See [docs/govcloud-deployment.md](docs/govcloud-deployment.md). (#525)

### Fixed

- **MCP `get_results` can now fetch a single document's results, and one broken tool no longer disables the whole MCP tool suite.** Two fixes for external MCP consumers (e.g. downstream gateways fed by post-processing hook events, which carry a document reference but no batch id): (1) `get_results` accepts a new `document_id` argument — the document's S3 object key or its `s3://` output-prefix URI — as an alternative to `batch_id`, backed by a new `idp_sdk` `client.batch.get_document_results()`; the gateway tool schema no longer hard-requires `batch_id`, and the schema is now re-synced onto existing gateway targets during stack updates (previously it was only sent at target creation, so schema changes never reached deployed gateways). (2) The MCP handler imported the analytics agent stack (`strands`) at module load, so a missing/incomplete agents Lambda layer failed the import of the whole handler — every tool died with `Runtime.ImportModuleError`. Tool modules are now imported lazily per call: batch tools work regardless, and `search` returns a structured error naming the missing dependency. See [docs/mcp-server.md](docs/mcp-server.md).

- **Multi-Doc Discovery container-image Lambdas no longer fail to pull their image on a stack update.** The `MultiDocDiscoveryEmbed`/`Cluster`/`Save`/`Analyze` functions are container-image Lambdas backed by a stack-created ECR repository that had **no repository policy** and whose execution roles carried **no `ecr:` pull actions** — the functions relied on Lambda auto-attaching a pull policy at function *create* time. That auto-attach is best-effort and is **not reapplied on update**, so when a later change forces the functions to update (e.g. the AppSync→DynamoDB env-var/IAM edit), the image re-pull fails with `The function does not have permission to access the specified image` and the functions never stabilize — failing the stack update. The ECR repository now declares an explicit `RepositoryPolicyText` granting the Lambda service (`lambda.amazonaws.com`, scoped to functions in this account/region) `ecr:BatchGetImage` and `ecr:GetDownloadUrlForLayer`, so the pull permission is durable across updates. (The CMK that encrypts the repo needs no change — Amazon ECR decrypts image layers server-side via a grant it creates on the key at repo-create time; the pulling principal does not call KMS.) See [nested/multi-doc-discovery/template.yaml](nested/multi-doc-discovery/template.yaml).

- **Pinned `ruff` in CI to keep the format gate reproducible.** Both CI lint jobs installed `ruff` unpinned (`uv pip install ruff`), so a newer release (0.16.0, which began reformatting Python code blocks embedded in Markdown) started failing `ruff format --check` on ~49 unchanged `.md` files repo-wide — unrelated to any PR's diff. `ruff` is now pinned to `0.15.13` (the version the repo is formatted against) in `.github/workflows/developer-tests.yml` and `.gitlab-ci.yml`.

- **S3 console folder creation no longer triggers spurious processing or pollutes Test Studio pattern matching.** Creating a "folder" in the input bucket via the S3 console writes a zero-byte pseudo-object whose key ends in `/` (e.g. `testfolder/`). The Queue Sender Lambda treated it as a document — creating a tracking entry and starting a Step Functions execution for empty content — and Test Studio's "Add Test Set from File Pattern" (`find_matching_files`) could pull such placeholders into a test set. Both paths now skip `/`-terminated keys (the trailing-slash guard already used elsewhere in the codebase). (#552)

- **Documents with `#` in their name no longer break View Data / View Page Text and results metadata.** `urllib.parse.urlparse` treats `#` in an S3 URI as a URL-fragment delimiter, so keys like `Report_#2.pdf/pages/1/result.json` were silently truncated at the `#`, causing `NoSuchKey` errors ("No response from getFileContents" in the UI) and wrongly-named `.metadata.json` files. The `getFileContents`/`getFilePresignedUrl` resolver and the two processresults functions now parse S3 URIs with a plain string split (matching `idp_common.utils.parse_s3_uri`), preserving `#` in keys.

- **Multi-doc discovery zip upload now works end-to-end.** The flow was broken two ways: `uploadMultiDocDiscoveryZip` returned a presigned **POST** form while the UI uploads with a raw HTTP **PUT** (so the zip never landed in S3), and the upload and start mutations each minted their own random job-id S3 path (so even a successful upload was looked up at a different key and the pipeline's Prepare step failed with a 404). The upload mutation now returns a plain presigned PUT URL, and both mutations derive the same deterministic key (`multi-doc-discovery/uploads/<fileName>`); callers that pass the returned `objectKey` back as `s3Prefix` are still honored verbatim.

- **Claude Sonnet 5 now appears in the UI model picker.** Sonnet 5 (`claude-sonnet-5` and the `:1m` extended-context variant) was present in pricing, model limits, and the Bedrock client routing — and is the system default extraction model — but was missing from the CloudFormation model enums in `patterns/unified/template.yaml` that feed the config schema, so it never showed in the config UI's model dropdown. Both variants are now registered across all three inference-profile prefixes (`us.`/`eu.`/`global.`). (#544)

- **Addressed package vulnerabilities flagged by recent dependency scans.** Upgraded affected direct and transitive dependencies across the web UI (`src/ui`), the docs site, and the multi-doc-discovery and OCR-benchmark Lambdas to their patched versions.

- **Added security response headers to the UI API (execute-api layer).** A DAST scan flagged the API Gateway `execute-api` endpoint for missing `X-Content-Type-Options`, `Strict-Transport-Security`, and `X-Frame-Options`, and a permissive CORS `Access-Control-Allow-Origin: *`. The CloudFront distribution that serves the SPA already sets these, but a client hitting the API directly did not receive them. The dispatcher's `_http_response` (the `/op` POST responses) and the API Gateway OPTIONS-preflight + 4xx/5xx gateway responses now emit `X-Content-Type-Options: nosniff`, `Strict-Transport-Security: max-age=31536000; includeSubDomains`, and `X-Frame-Options: DENY` (plus `Referrer-Policy` on the Lambda responses). The CORS wildcard stays the default (it is safe — the UI sends a Bearer JWT, never credentials/cookies, and a non-wildcard origin would create a CloudFront→API CloudFormation dependency cycle). A `CORS_ALLOW_ORIGIN` env var is available as a **code-level hook** (read by the dispatcher; not wired to a stack parameter) for forks that pin the Lambda's environment to lock the `/op` origin down. In `ServeWebUI` mode (API Gateway hosts the SPA, no CloudFront), the SPA document/asset responses also carry the security-header trio.

- **Closed the Agent Chat authorization gap (Reviewer exclusion now server-side, not UI-only).** `sendAgentChatMessage` and `listAvailableAgents` restricted Agent Chat to Admin/Author/Viewer only through UI navigation — a Reviewer calling the API directly was accepted. Both resolvers now enforce the group server-side via a `_caller_in_groups` check (matching `calculateCapacity`), and `schema.graphql` declares the real `@aws_cognito_user_pools(cognito_groups:["Admin","Author","Viewer"])` restriction. The original constraint (AppSync could not combine a `cognito_groups` restriction with `@aws_iam` on one field) no longer applies now that AppSync has been removed; the IAM backend publish path carries no Cognito identity and is unaffected. Previously tracked as accepted-risk GAP-03 in the RBAC test register; now hard-gated (a regression fails `make api-test`). See the [api-rbac-test skill](.claude/skills/api-rbac-test.md) and [docs/rbac.md](docs/rbac.md).

- **Closed three API authorization gaps (server-side RBAC now matches the schema).** Three operations previously relied on a schema directive that the REST dispatcher's Cognito authorizer does not enforce, with no matching server-side check: (1) **`listUsers`** was readable by any authenticated user, exposing every user's email and role to Viewers/Reviewers — now Admin-only (server-side check + `@aws_cognito_user_pools(cognito_groups:["Admin"])`); (2) **`calculateCapacity`** had no server-side group check despite a schema directive, so a Reviewer calling it directly succeeded — now enforces Admin/Author/Viewer server-side; (3) the feature-platform ops **`subscribeFeature`/`unsubscribeFeature`/`getFeatureLaunchUrl`** declared the silently-ignored `@aws_auth` directive (or none) — now declare `@aws_cognito_user_pools(cognito_groups:["Admin"])` matching the Admin enforcement their resolvers already performed (defense-in-depth; these three were not exploitable). All were previously tracked as accepted-risk GAP-04/05/06 in the RBAC test register; they are now hard-gated (a regression fails `make api-test`). Threat model updated (AUTH.T08). See the [api-rbac-test skill](.claude/skills/api-rbac-test.md).

- **GovCloud deployments now succeed end-to-end out of the box.** A batch of GovCloud (`us-gov-west-1`) fixes, all verified live against a real deployment: (1) the `--govcloud` template transform now defaults `KnowledgeBaseVectorStore` to `OPENSEARCH_SERVERLESS` (S3 Vectors is unsupported for Bedrock Knowledge Bases in GovCloud), `KnowledgeBaseModelId` to a GovCloud-verified model (`amazon.nova-pro-v1:0` — the commercial `us.`/`eu.`/`global.` cross-region inference-profile prefixes are all invalid there), and `ConfigurationPreset` to `lending-package-sample-govcloud` (the commercial preset pins Bedrock models that don't exist/aren't offered in GovCloud); (2) the `lending-package-sample-govcloud` preset now pins GovCloud-invokable models across every stage (summarization, chat, evaluation via `llm_method.model`, discovery/auto-split/rules, and the agents) instead of retired Claude 3.x IDs, and adds a self-healing `escalation_model`; (3) the `CognitoAuthorizedRole` trust policy now lists the GovCloud Cognito Identity federated principals (login failed with `InvalidIdentityPoolConfigurationException` without them — the commercial principal passes CFN validation silently but never lets the identity pool assume the role); (4) S3 Vectors ARNs are now built from the deploy region's partition instead of a hardcoded `arn:aws:`; (5) the AgentCore gateway falls back to a no-semantic-search gateway (semantic search is unsupported in GovCloud) instead of failing the stack; (6) the main-template cfn-lint pre-check is skipped for `--govcloud` publishes (the un-transformed template contains UI/CloudFront/Cognito resources that don't exist in GovCloud regions), matching the existing `--headless` behavior; and (7) the HTTP-API field→resolver SSM parameter is de-duplicated to one entry per resolver Lambda (with duplicate fields routed via dispatcher aliases) so it fits the 8 KB Advanced-tier limit once the longer `arn:aws-us-gov:` ARNs are resolved — this last fix also benefits commercial deployments. GovCloud-verified `KnowledgeBaseModelId` entries and matching pricing entries were added. See [docs/govcloud-deployment.md](docs/govcloud-deployment.md).

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.6.2.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.6.2.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.6.2.yaml`
  

## [0.6.1]

### Added

- **OpenAI GPT-5.6 (Sol / Terra / Luna) models on Amazon Bedrock.** Adds `openai.gpt-5.6-sol` (flagship reasoning), `openai.gpt-5.6-terra` (GPT-5.5-class quality at roughly half the cost), and `openai.gpt-5.6-luna` (fastest / lowest cost), served on the `bedrock-mantle` OpenAI Responses API alongside the existing GPT-5.4/5.5. Selectable for OCR, classification, extraction, assessment, summarization, evaluation, and Chat-with-Document. Unlike 5.4/5.5 (which cache automatically), GPT-5.6 supports **explicit prompt caching** via `<<CACHEPOINT>>` markers (90% cache-read discount; a 30-minute cache-write is metered). **Not supported:** agentic extraction, Discovery, and Policy Discovery (rejected by `config-validate` and at runtime). **Regions:** US only — Sol in `us-east-1`/`us-east-2`, Terra/Luna add `us-west-2`; no EU/global and **no GovCloud** (GovCloud remains GPT-5.4 only). See [OpenAI GPT-5.x Models](docs/openai-models.md). (#519)

- **Blogs, Customer Stories & Research references page.** A new [references doc](docs/references.md) collects and summarizes external publications about the accelerator: AWS ML blog feature deep-dives (including automated schema generation / multi-document discovery), customer reference stories (Myriad Genetics, Associa, Ricoh, Built Technologies) with real-world accuracy/cost/throughput results, and the peer-reviewed research papers (the IDP Accelerator arxiv paper and DocSplit, both ACL 2026). Linked from the docs index and the Starlight sidebar. (#507)

- **Error Analyzer agent can name the exact model behind a Bedrock model error.** A new `fetch_pipeline_configuration` tool returns the Bedrock model ID configured for each pipeline stage of a document's config version. Because a retired/unavailable-model error (`ResourceNotFoundException` — "This model version has reached the end of its life") does not name the model, the agent previously could only tell the user to "go confirm the configured model". It now reads the failing stage's model and names it definitively (GitHub #504 follow-up).

- **Simple extraction now drops off-schema fields before assessment.** Traditional (non-agentic) extraction previously kept whatever the model returned (raw `json.loads`), so a hallucinated or cross-class attribute (e.g. a `resume` section coming back with a `publications` list) flowed into `inference_result` and later broke confidence assessment. A new schema-compliance filter drops top-level fields not defined in the class schema right after parsing — mirroring what Advanced (agentic) extraction already does via its Pydantic model — at zero extra model cost. Dropped fields are surfaced as an `extraction_off_schema_fields` (info) processing issue. See the [extraction README](lib/idp_common_pkg/idp_common/extraction/README.md#schema-compliance-filtering-simple-mode) (GitHub #510).

### Changed

- **BDA-as-OCR project is now a per-stack resource, not an account-global one.** For the `bda` OCR backend, the standard-output SYNC project was auto-created at runtime under one hardcoded, account-wide name (`GENAIIDP-OCR-StandardOutput`), so multiple stacks in an account interfered with a single shared project. Each stack now provisions its own `<stackname>_OCR_StdOutput` project via a CloudFormation custom resource (`Custom::BDAOCRProject`) — created and deleted with the stack — and the OCR function no longer creates it at runtime. **Migration:** pre-existing `GENAIIDP-OCR-StandardOutput` projects are now orphaned (harmless) and can be deleted manually once all stacks are upgraded. See the [OCR README](lib/idp_common_pkg/idp_common/ocr/README.md).

- **Page viewer unified into one Visual Editor layout with an OCR-lines / Markdown toggle.** The Web UI "View/Edit Page Text" modal previously had three separate tabs (Visual Editor, Text + Markdown, Text + Confidence). It now always shows the page image on the left (when available) with a right-pane toggle between **OCR Lines** (clickable → color-coded confidence bounding box, as before) and **Markdown** (with a Rendered ↔ Raw sub-toggle; Raw is the editable surface in edit mode). The separate **Text + Confidence** tab is retired — its per-line text+confidence is already shown, color-coded, in the OCR Lines pane (and it was inoperative anyway, as `TextConfidenceUri` was never persisted to the tracking table). Page-text editing is preserved via the Raw markdown editor. See [docs/web-ui.md](docs/web-ui.md).

- **Summarization failures now surface the underlying error, not a generic wrapper.** When a section's summarization fails (e.g. a Bedrock `ResourceNotFoundException` from a retired model, or a context-window overflow), the Summarization Lambda now includes the real cause in the exception it raises to Step Functions instead of a bare `"Summarization failed for document X"`, so the failure is diagnosable from the execution error and tracking record without opening CloudWatch (GitHub #504).

- **Error Analyzer agent drills into the failing stage's own logs and no longer conflates non-fatal issues with the failure.** The troubleshooting prompt now explicitly instructs the agent to treat stage-level wrapper messages as symptoms and read the first underlying exception in the failing Lambda's log group, to not attribute a `FAILED` workflow to unrelated non-fatal `ProcessingIssue` warnings from a different stage, and (for a Bedrock model error) to call `fetch_pipeline_configuration` and name the exact configured model rather than telling the user to check it.

- **Bedrock client logs the model ID on non-retryable errors.** Non-retryable `Converse` and embedding failures now include the model ID in the log line (e.g. `Non-retryable Bedrock error for model us.anthropic.claude-3-7-sonnet-20250219-v1:0: ResourceNotFoundException - ...`), so the offending model is explicit in the function logs — Bedrock's own message for a retired model does not name it.

### Fixed

- **Feature Platform stack now honors `PermissionsBoundaryArn`, fixing rollback in SCP-enforced accounts.** When `EnableFeaturePlatform=true`, the main template did not forward `PermissionsBoundaryArn` to the Feature Platform nested stack (`feature-platform/main-stack-extensions`), and that nested template neither declared the parameter nor attached a boundary to its `FeaturePlatformLambdaRole` — so in accounts whose SCP requires a permissions boundary on every IAM role, `iam:CreateRole` was denied and the nested stack rolled back on creation. The parameter is now declared in the nested template, attached to the role, and forwarded from the main stack. The same gap on the SAM-auto-role Lambda functions in the installable feature templates (`feature-template`, `sample-feature`, `sample-health-insurance-review`) and on the conditional `BastionRole` / AgentCore roles/function in the main template is fixed as well. A static regression test (`lib/idp_sdk/tests/unit/test_permissions_boundary_coverage.py`) now asserts every deployed-stack template attaches the boundary to each role it creates and forwards the parameter to every nested stack that accepts it. (#521)

- **A failed stack update no longer wedges the automatic rollback on the config custom resource.** When an in-place update from a pre-0.6 release (e.g. 0.5.16) to 0.6.x failed for any reason and CloudFormation rolled back, the rollback itself got stuck in `UPDATE_ROLLBACK_FAILED`. The config custom resource (`UpdateConfigurationFunction`) had already migrated the stored config to the 0.6 shape; on rollback CloudFormation reverts that Lambda to the *older* release's code and re-invokes it, which then re-read the now-0.6 config and crashed — a bare `int(None)` `TypeError` on fields 0.6 stores as `null` (e.g. `max_tokens` = "request model max"), and a `gt=0` `ValidationError` on fields 0.6 stores as `0` (e.g. `extraction.agentic.shard_token_budget` = "auto-size"). Two fixes: (1) the DynamoDB serializer now **omits scalar fields whose value equals their default and is `None` or integer `0`** — behavior-neutral for the current model (absent == default on read) but sparing the reverted older model from values it cannot parse (verified: every bundled config now parses cleanly under the 0.5.16 model, with a lossless 0.6 round-trip); and (2) the custom resource now **detects a rollback** (a stored `config_format_version` newer than the running code's) and returns SUCCESS instead of FAILED on a parse error, letting the rollback complete rather than wedging the stack — while a genuine forward bad-config still fails loudly. Note: only managed/default config records are rewritten at deploy time; user Custom and named config versions are untouched. See the [config module README](lib/idp_common_pkg/idp_common/config/README.md).

- **OpenAI (bedrock-mantle) metering no longer double-counts cached tokens.** OpenAI's Responses `usage.input_tokens` is the *total* prompt count and already **includes** the cached / cache-write tokens as a subset — unlike Bedrock Converse, where `inputTokens` is the disjoint uncached count. The mantle usage mapper reported that total as `inputTokens` while *also* emitting `cacheReadInputTokens` / `cacheWriteInputTokens`, so the cost calculator billed cached tokens twice (full input rate **and** cache rate) — turning the cache discount into a ~64% overcharge on warm calls. `inputTokens` is now the disjoint fresh count (`input_tokens − cached − cache_write`), so `inputTokens + cacheReadInputTokens` reconciles to the true prompt size. Affects all GPT-5.x models (5.4 / 5.5 / 5.6). Verified live end-to-end. See the [bedrock module README](lib/idp_common_pkg/idp_common/bedrock/README.md#openai-gpt-5x-models-bedrock-mantle-responses-api). (#519)

- **Addressed package vulnerabilities flagged by recent dependency scans.** Upgraded affected direct and transitive dependencies across the Python library (`lib/idp_common_pkg`), the fcc dataset deployer Lambda, the web UI (`src/ui`), the docs site, and the feature-platform UIs to their patched versions.

- **BDA-as-OCR no longer drops table-cell text/confidence/geometry from `pageData.json`.** When Amazon Bedrock Data Automation is used as the OCR engine (`ocr.backend: bda`), BDA leaves the line-level text empty on table-cell lines (the content lives only in the child words). The BDA→Textract-block converter copied that emptiness onto the LINE block, and the shared `pageData.json` builder drops empty-text LINE blocks — so all table-cell content (and its confidence/geometry) was silently stripped from the Page viewer's OCR lines and from `textConfidence.json`, even though it rendered fine in the extracted markdown. The converter now synthesizes the line text from its child words, so table cells survive with their word-level confidence and bounding boxes intact (verified: recovers all lines to match the Textract OCR variant). See the [OCR README](lib/idp_common_pkg/idp_common/ocr/README.md). (#513)

- **`rvl-cdip-package-sample` no longer fails at the Summarization stage on a retired model.** The config pinned `us.anthropic.claude-3-7-sonnet-20250219-v1:0` for OCR and Summarization, which Bedrock has since retired (end-of-life); every Summarization `Converse` call returned `ResourceNotFoundException` and failed the whole document. The preset is now reduced to only its custom `classes` (plus a `notes` label) and inherits every other section — OCR, classification, extraction, assessment/confidence, summarization, evaluation, discovery, and agents — from the current system defaults, so it always uses supported models and the latest prompts instead of drifting onto retired ones.

- **Error Analyzer agent can now read stack logs on Lambda cold starts.** The SSM settings cache (`monitoring/settings_cache.py`) used `time.monotonic()` against a `0.0` "never-loaded" sentinel to decide expiry. On a cold-start Lambda microVM, `monotonic()` is often below the cache TTL, so the never-loaded cache was wrongly treated as fresh, the SSM refresh was skipped, and settings (including `CloudWatchLogGroups`) came back empty — the agent then reported "log groups inaccessible / not found in SSM Settings" and could not find the underlying error. The cache now treats a never-loaded state as always-expired regardless of the monotonic clock (GitHub #504 follow-up).

- **Assessment no longer wastes a model escalation (or hard-fails a document) on a schema mismatch.** When extraction returned list-valued data for an attribute the class schema does not define as an array, the confidence enhancer collapsed it to a single default leaf, leaving every row "unscored" — and the self-healing ladder then escalated to a stronger confidence model that re-emitted the same list and recovered **0 rows**, marking the section `assessment_incomplete` (error) and failing the document. The ladder now detects this schema mismatch, **skips both retry and escalation** for the field (a stronger model cannot fix a schema mismatch), and emits a clear `assessment_schema_mismatch` (error) processing issue naming the field(s) and the real fix (correct the schema/extraction, or use Advanced extraction) instead of a misleading "escalation failed" story. Paired with the simple-mode off-schema filter above, the offending field is normally dropped at extraction so the section no longer fails at all. See the [assessment README](lib/idp_common_pkg/idp_common/assessment/README.md) (GitHub #510).

- **HITL "Skip All Reviews" now finalizes the document like completing the last section.** Skipping all pending sections previously updated tracking status but did **not** trigger downstream reprocessing, so Summarization/Evaluation never re-ran and the optional post-processing Lambda hook (`PostProcessingLambdaHookFunctionArn`) never fired — inconsistent with the section-by-section completion path, which does. `skip_all_sections_review` now calls the same `trigger_reprocessing` used when the final section is completed, so both "finish review" paths behave identically. See [docs/human-review.md](docs/human-review.md).

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.6.1.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.6.1.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.6.1.yaml`

## [0.6.0]

This release (v0.6) reframes **per-field confidence and bounding-box geometry as
outputs of extraction** rather than a separate assessment stage, retires the
granular assessment service, adds robust large-list / large-document handling, and
completes the AWS AppSync → API Gateway transport migration. **Existing v0.5 configs
are migrated automatically on read — no manual edit is required.** See the
[Extraction & Confidence guide](docs/extraction-and-confidence.md), the
[Granular Assessment Retirement note](docs/migration-granular-retirement.md), and the
[AppSync → REST migration guide](docs/migration-appsync-to-rest.md).

### Added

- **Integrated confidence — per-field confidence produced inside extraction, no separate assessment pass.** `confidence.mode: integrated` emits each value's confidence in the extraction inference itself (then enriches with per-field thresholds/alerts, grounds geometry, and writes the same `explainability_info` contract, so downstream evaluation/reporting/UI/HITL are unchanged and the standalone Assessment step auto-skips). On Simple mode this uses a single **1S-TopK** call that asks the model for its top-K guesses with probabilities — better-calibrated than single-value self-assessment — halving extraction-workflow LLM calls. Works on both the Advanced (agentic) and Simple paths. See the [Extraction & Confidence guide](docs/extraction-and-confidence.md#confidence-mode-separate-vs-integrated-vs-off).

- **Reliable, low-cost confidence assessment for large lists.** Large-list confidence is now complete and cheap on the first pass: batch sizes are auto-derived from the confidence model's output cap (so a small-cap model like Nova Lite no longer truncates), independent batches run concurrently after one cache-warming call, page images are dropped from the prompt in OCR-geometry modes, truncated responses are salvaged (valid prefix kept, only the remainder re-scored), and any still-unscored rows escalate to a stronger model. All of this is bounded by the Lambda wall-clock budget and checkpointed to S3 so a shard resumes without repeating extraction. Activity is recorded in `assessment_batch_split_stats` and the processing report. See the [assessment README](lib/idp_common_pkg/idp_common/assessment/README.md).

- **Document versions — every processing run is retained, comparable, and downloadable.** Re-uploading or reprocessing a document (same S3 key) no longer discards prior results: each successful run is snapshotted as an immutable version (pinned S3 object versions in a per-run manifest). The document detail page gains a **Version History** panel to view, compare, and (Admin-only) delete past versions; the CLI adds `idp-cli list-versions` and `download-results --run-id`; and the API adds `listDocumentVersions`/`getDocumentVersion`/`compareDocumentVersions`/`deleteDocumentVersion`. Enabled by default, retained for `DataRetentionInDays`. See the [Document Versions guide](docs/document-versions.md).

- **Quick Start — cold-start configuration authoring for new deployments.** A conversational agent bootstraps a working configuration for first-time users: describe your document type in natural language, or attach example documents to run Discovery and infer document classes automatically. It authors an extraction schema, matches bundled sample configs (citing them as inline links to the real documents), and writes a new configuration version you can refine in chat. Surfaced via a floating widget and a Welcome page; gated by `EnableQuickStartWidget`; also available from the CLI (`idp-cli bootstrap`). See the [Quick Start guide](docs/quick-start.md).

- **API Gateway Web UI hosting (`WebUIHosting=APIGateway`) — private/VPC-capable UI without CloudFront.** A new hosting option serves the React SPA as an S3 proxy on the existing api-resolvers REST API, on the same stage as the data operations, so the UI inherits the API's network posture: set `ApiGatewayVisibility=PRIVATE` (with `DeployInVPC=true` + VPC params) for a VPC-only UI, and the optional WAF IP allow-list covers it too. No ALB, ACM certificate, or S3 VPC endpoint required. See the [API Gateway Hosting guide](docs/apigateway-hosting.md).

- **`--govcloud` publish/deploy option — full Web UI in GovCloud.** `idp-cli publish`/`deploy` gain a `--govcloud` flag that transforms the template to remove every CloudFront resource (unavailable in GovCloud) and forces `WebUIHosting=APIGateway`, serving the full interactive Web UI as an S3 proxy on the REST API — unlike `--headless`, which removes the UI entirely. Chat still works without live streaming (the UI polls for the final answer). The tooling runs a region-aware `cfn-lint` on the transformed template and fails the build on any GovCloud-unsupported-resource error. Requires the API Gateway hosting option. See the [GovCloud Deployment guide](docs/govcloud-deployment.md#keeping-the-web-ui-in-govcloud---govcloud).

- **BDA as a pure OCR engine (`ocr.backend: bda`).** Amazon Bedrock Data Automation "standard output" can be used as an OCR-only engine feeding the normal classification/extraction pipeline — distinct from whole-pipeline BDA mode (`use_bda`). It returns reading-order markdown with tables/layout plus word-level confidence and bounding boxes at a flat $10/1,000 pages, auto-enables the agentic table tool, and is adapted to Textract response format so it flows through the existing confidence/geometry path. Choose it for table-heavy documents and predictable pricing. See the [OCR README](lib/idp_common_pkg/idp_common/ocr/README.md).

- **Lower-cost agentic extraction on table-heavy documents (up to ~57% cheaper, no accuracy change).** The deterministic table tools now return only a compact model-visible summary (full rows kept in agent state, read directly by downstream tools) so dense rows are no longer re-transmitted every turn, and a new `extraction.agentic.table_parsing.lazy_images` knob (default `true`) skips pre-loading page images when a table parse succeeds (the agent can still fetch a page on demand). Set `lazy_images: false` for image-dependent corpora. See the [Extraction & Confidence guide](docs/extraction-and-confidence.md).

- **Bounding-box OCR grounding is ~30× faster on large tables (indexed match).** Grounding now builds a cached normalized-line list + an exact-text index per page and does an index-first pass, falling back to the fuzzy ladder only when nothing matched verbatim — cutting a 1,440-row section from ~64s to ~2s with byte-identical output.

- **Claude Sonnet 5 is selectable everywhere** (`us`/`eu`/`global.anthropic.claude-sonnet-5`, plus `:1m` extended-context variants) — registered in the model picklists, pricing, output-token limits (128K), prompt-cache support, and Bedrock TPM quota codes. Sonnet 5 rejects non-default sampling params, so IDP strips them automatically. **Request Anthropic Claude Sonnet 5 access in Bedrock before selecting it.**

- **Reasoning effort is configurable for reasoning-capable Claude models (extraction defaults to `low`).** `reasoning_effort` drives Claude's `output_config.effort` on the Bedrock Converse path for effort-capable models (Sonnet 5, Sonnet 4.6, Opus 4.5–4.8, Fable 5), wired through both standard and agentic extraction; the config UI shows the selector only when the model supports it. A full sweep found higher effort adds output-token cost with negligible accuracy gain, so `low` keeps the Sonnet 5 default affordable.

- **Processing transparency — structured issues surfaced in the UI and report.** A new `ProcessingIssue` concept (stage, severity, code, message, root cause) is attached to each section, rolled up to the document, and rendered in the web UI (a Status column on the Sections panel with a hover popover, a Processing Issues column on the document list) and at the top of the section Processing Report. So a document that self-healed with retries — or one where a row genuinely couldn't be scored — is visible at a glance instead of only in `result.json`. The report also surfaces how the document was processed (model window, context buffer, auto-derived shard/batch sizes, batch counts + concurrency, a process-flow visual), and an under-producing extraction is honestly flagged `extraction_incomplete` rather than a misleading SUCCESS.

- **Bundled sample documents are deployable and launchable from the web UI, with one-click matching configuration.** The curated `samples/` documents are published to the ConfigurationBucket at deploy time. The Upload Documents panel gains a Document source toggle to pick a bundled sample and process it without downloading it first; when a sample is tuned for a Configuration Library preset that isn't imported yet, a pre-checked option imports that preset as a new config version and processes the sample with it. New operations: `listSampleDocuments`, `uploadSampleDocument`. See [web-ui.md](docs/web-ui.md#upload-documents).

- **In-app feedback & GitHub issue reporting.** Users can report bugs or request features on the project's public GitHub repository directly from the web UI, with deployment details and document/agent context pre-filled into the issue body. Entry points: a Feedback menu in the top nav, a "Report this issue on GitHub" button in the Error Analyzer Troubleshoot modal, a "Create GitHub issue" button in the Agent Companion Chat, and side-nav/help-panel links. Nothing is submitted automatically — GitHub always shows the pre-filled form to review first, with a reminder to redact sensitive data. See [web-ui.md](docs/web-ui.md#feedback--issue-reporting). (#489)

- **View/Edit Configuration UX improvements.** The config editor de-clutters its toolbar (secondary actions collapse into an Actions menu), adds tooltips explaining disabled actions, shows the unsaved-changes banner + Discard on all versions, restores deep-link URLs on navigation, renders prompts in taller textareas with an Expand-editor modal, requires a confirmation modal to delete a version, and shows a word-level inline diff when comparing two versions. The Document Schema builder adds Export all/selected, an "Add another" attribute flow, and a graphical entity-relationship Diagram preview of the classes. See [web-ui.md](docs/web-ui.md) and [configuration-versions.md](docs/configuration-versions.md). (#482, #484)

- **Discovery: create a new configuration version inline, and a "Replace" save mode.** The Discovery panels gain a **Create new version** modal (inheriting settings and classes from a chosen source version) and a **Save mode** control: *Add to existing schema* (default) or *Replace existing schema*, which clears the target version's document classes once up front before saving only the newly discovered ones. Threaded through the discovery mutations via a new optional `saveMode` argument. See the [Discovery guide](docs/discovery.md#web-ui-interface).

- **"View / Edit Model Limits" page — per-model token limits editable at runtime.** The ordered, first-match-wins model limits list (`config_library/model_config_limits.yaml`) is now seeded into the DynamoDB Configuration Table at deploy and surfaced in a new Admin-editable page (Table/JSON/YAML views, row reordering, import/export, restore-defaults, a "Test a model ID" tool). Edits take real effect via a 60-second runtime cache; `config_library/model_config_limits.yaml` is now the single source of truth (the copy bundled in the wheel is removed). New operations: `getModelConfigLimits`, `updateModelConfigLimits`, `restoreDefaultModelConfigLimits`.

- **`DATE` evaluation method for format-insensitive date comparison.** Upgraded the evaluation engine to `stickler-eval` 0.5.0 and exposed its `DateComparator` as a first-class method (`x-aws-idp-evaluation-method: DATE`): it parses both values into dates before comparing, so `01/05/2024`, `2024-01-05`, and `January 5, 2024` all match, and understands date ranges. All `format: date` fields across the bundled presets are switched to it (time-only and free-text fields intentionally left on their prior methods). See the [evaluation guide](docs/evaluation.md).

- **`idp-cli deploy --tags "key=value,..."`** — applies CloudFormation stack-level tags at deploy time, propagated to all nested stacks/resources for governance and cost allocation. See [Resource tagging](docs/idp-cli.md#resource-tagging).

- **`idp-cli config-validate --emit-migrated <path>`** — writes a config migrated to the current format (v0.5 → v0.6) to a file so you can review the v0.6-shaped result before importing. See [config-validate](docs/idp-cli.md#config-validate).

- **CLI/SDK: promote a processed document to the evaluation baseline.** New `idp-cli use-as-baseline --stack-name <s> --document-id <key>` command and `client.evaluation.use_as_baseline(...)` SDK method — the scriptable equivalent of the UI button; runs synchronously and returns the file count.

- **API RBAC test system — static scan + live authorization harness (`make api-test`).** A two-layer, auditable test system that verifies every UI API operation is protected by its required Cognito group(s) and, where applicable, config-version scope. `make api-test-static` runs with no AWS (CI-safe), cross-checking the operation universe against schema directives and a single source-of-truth expectations file; `make api-test STACK_NAME=<stack>` drives a deployed stack with temporary per-role Cognito users (plus unauthenticated and tampered-token calls) and writes a JSON/Markdown report. Documented as a skill (`.claude/skills/api-rbac-test.md`); this harness found the config-version scope fail-open fixed in this release.

- **Feature SDK: shell-free structured build/package commands in `feature.yaml`.** The feature manifest gains structured step-list forms `ui.build` and `agentSource.package` — each step an `{argv, cwd}` object executed with `shell=False` — eliminating the shell command-injection surface (Bandit B602). The legacy single-string command forms remain supported but are deprecated. See [the SDK README](lib/idp_feature_sdk/README.md#build--package-commands-in-featureyaml).

- **API Gateway logging at `LogLevel` INFO/DEBUG (metadata only, no payloads).** When `LogLevel` is INFO or DEBUG, the web UI's REST API stage enables CloudWatch **access** logging (request metadata only — never bodies — capturing failures before the dispatcher: authorizer 401/403s, WAF blocks, CORS/gateway responses) and **execution** logging at `LoggingLevel: ERROR` with `DataTraceEnabled: false` (deliberately not INFO, which would echo document payloads). Logs go to stack-managed, KMS-encrypted groups honoring `LogRetentionDays`. At WARN/ERROR (recommended for production) no access logging is configured, matching prior behavior. Resolves SRT finding API-GW-006. (#481, #488)

- **CI coverage: package/Lambda test suites and the GovCloud ARN check now run in CI.** Two gaps closed. The MR `developer_tests` job previously ran only the `idp_common_pkg` suite plus the UI vitest tests; a new `make test-packages-cicd` target adds the ~665 tests that were green headless but never exercised in CI — `idp_cli_pkg`, `idp_sdk`, `idp_feature_sdk`, the feature-platform resolver/template suites, the capacity-planning and circuit-breaker Lambdas, the chat processors, and the `config_library` validation — so a regression in any of them fails the MR. Separately, the `check-arn-partitions` guard (which fails on hardcoded `arn:aws:` / service principals that break GovCloud) was wired into `lint-cicd`; it had been present in the local `lint`/`fastlint` targets but not in the target CI runs, so a hardcoded partition could previously pass CI.

- **CI pipeline: fail-fast security gate, automatic root-cause analysis for failed deployments, failure email, and API RBAC tests in the gate.** Four CI improvements to the GitLab + CodeBuild test pipeline. (1) The SRT security scan now runs on the same branch pipelines as the integration tests (develop/feature/fix — previously it only ran on MR pipelines, so the two never shared a pipeline) and is ordered before the ~2h `integration_tests` stage with `needs: []`, so a pipeline that will fail the security gate fails in minutes, not hours. (2) The Bedrock-generated deployment summary now always explains *why* a run failed: the Step 4b API Gateway/VPC hosting test — which previously ran *after* the summary was generated, so its failures produced a bare "deployment failed!" line with a stale all-green summary — gets its own AI root-cause section from CloudFormation failure events captured *before* the throwaway stack is torn down; the GitLab job's CodeBuild log fetch is fully paginated (a single `get-log-events` call truncates 2h builds); and the build uploads the summary to S3 under a deterministic key (`deploy/summaries/<build-id>.txt`) so the GitLab job reads it directly instead of scraping logs (`deployment_summary.txt` is now a job artifact). The final failure line includes the failure reason (e.g. `💥 Stack: … deployment failed! — API Gateway/VPC hosting deploy failed`). (3) A new optional `FailureNotificationEmail` parameter on the SDLC pipeline template creates an SNS topic + email subscription; the build publishes the AI failure summary to it, so the pass/fail email contains the root cause. (4) The API RBAC tests join the gate at both layers: `make api-test-static` runs in the MR `developer_tests` job (no AWS needed), and the live harness runs as a new sequential Step 12 (`make api-test`) against the freshly deployed integration-test stack (sequential because it temporarily toggles the Cognito app-client auth flows); the pipeline's CodeBuild role gains the scoped Cognito permissions the harness needs.

### Changed

- **Config format v0.6: confidence & geometry are outputs of extraction; HITL is its own section (auto-migrated on read).** The former top-level `assessment` block is retired as the home for confidence scoring — settings move under `extraction.confidence.*` / `extraction.geometry.*`, and `assessment.hitl_*` moves to a new top-level `hitl.*`. Existing v0.5 configs are migrated automatically by an idempotent transform at both config-import and deploy-time merge, so custom configs and presets keep working unchanged. The config UI is reorganized to match (top-level Extraction mode, Confidence/Geometry mode selectors, and a new HITL Review section). See the [Extraction & Confidence guide](docs/extraction-and-confidence.md).

- **AWS AppSync fully removed — replaced by an API Gateway REST API + polling + Lambda response streaming.** The UI↔backend transport no longer uses AppSync (unavailable in GovCloud, not FedRAMP-compliant). Queries/mutations go through a REST API with a Cognito authorizer (supporting `ApiGatewayVisibility=PRIVATE` and an optional WAF WebACL); status updates poll; the two chat flows stream via a Lambda Function URL. Parameters `AppSyncVisibility`/`UsePrivateAppSync` are renamed to **`ApiGatewayVisibility`/`UsePrivateApi`**. See the [AppSync → REST migration guide](docs/migration-appsync-to-rest.md).

- **ALB Web UI hosting (`WebUIHosting=ALB`) removed — replaced by API Gateway hosting.** The internal-ALB hosting mode, the `nested/alb-hosting/` stack, and all `ALB*` parameters have been deleted. **Migration:** switch `WebUIHosting=ALB` → `WebUIHosting=APIGateway`; for the equivalent private posture also set `ApiGatewayVisibility=PRIVATE` + `DeployInVPC=true` and the VPC params. No ACM certificate, ALB, or S3 VPC endpoint needed. The application URL changes from the ALB DNS name to the execute-api `/api/` URL. See the [API Gateway Hosting guide](docs/apigateway-hosting.md).

- **Granular assessment is retired and its service deleted.** The `GranularAssessmentService` and the `extraction.confidence.granular` config field no longer exist. Large-list assessment is now handled by standalone batching (`extraction.confidence.list_batch_size`, default 25) with a bounded missing-row retry to reach 100% per-cell coverage — ~78% cheaper than granular on a 120-row bank statement with equal accuracy. **Migration is a no-op:** leftover `granular.*` keys still validate and are ignored. See [Granular Assessment Retirement](docs/migration-granular-retirement.md).

- **Default extraction model is now Claude Sonnet 5; default confidence model is Amazon Nova Lite.** Sonnet 5 gives the best extraction quality; confidence stays on the far cheaper Nova Lite because a live A/B measured ~$0.0011/doc vs ~$0.145/doc (~130×) for the assessment pass, where output tokens dominate cost. Presets that pin an explicit model are unaffected. **Request Anthropic Claude Sonnet 5 access in Bedrock before upgrading.**

- **Advanced (agentic) extraction shards large documents reliably on a Step Functions Distributed Map.** Sharding engages by default on large docs (token and page-count bounds per shard), each shard persists idempotently to S3 so SFN retries re-run only failed shards, and confidence assessment + bounding-box grounding run inside each shard (scaling with extraction) before a merge step reconciles to the exact row count. Shard token budgets and confidence batch sizes are derived from each model's context/output window minus a single `extraction.context_buffer` (default 30%), so per-model hand-tuning is no longer required. See the [Extraction & Confidence guide](docs/extraction-and-confidence.md#large-document-guidance).

- **`max_tokens` is now optional everywhere — leave it empty to use the model's maximum output limit.** Every service treats `max_tokens` as an optional cap resolved from `model_config_limits.yaml` when unset; extraction and confidence always request the model's maximum (their `max_tokens` fields are removed — leftover values ignored, no migration needed) because completeness matters more than an output cap. Set a positive value only to cap below the model max. In the config UI the advanced inference params are grouped into a collapsed "Model parameters" section. See [Configuration](docs/configuration.md).

- **OCR-only geometry is the default (`geometry.mode: ocr_only`).** Field bounding boxes are derived by matching extracted values to real OCR lines in `pageData.json` instead of asking the model for coordinates — cheaper (no bbox tokens) and more accurate (no hallucinated boxes); repeated values are disambiguated by row order and matching is format-aware. LLM-box modes (`llm`, `llm_grounded`) remain available. See the [Extraction & Confidence guide](docs/extraction-and-confidence.md#geometry--bounding-boxes).

- **Textract `TABLES` is enabled by default in OCR (`ocr.features: [TABLES, LAYOUT]`).** Tables are common and the accuracy gain is large: on a 24-page brokerage statement `TABLES` extracted all 1,440 rows where `LAYOUT`-only silently dropped ~5 pages. `TABLES`+`LAYOUT` (~$0.065/page) costs ~16× the Textract line-item of `LAYOUT`-only but is typically more cost-effective end-to-end for documents *with* tables. For a table-free corpus, remove the `TABLES` entry to fall back to cheaper `LAYOUT`-only. See [Configuration → Textract features](docs/configuration.md).

- **Confidence prompts emit `confidence_reason` only for lower-confidence leaves (below 0.9), capped at ~12 words.** A high-confidence value returns just `{"confidence": <score>}`. Because output tokens dominate assessment cost, this cuts output substantially with no change to scores, thresholds, or alerts. To restore a reason on every field, widen the 0.9 threshold in the confidence `task_prompt`.

- **Config editor now shows per-step guidance and a consistent enable-toggle label.** Each processing-step section renders a short description explaining what it does and when it is skipped, the deterministic table parser's OCR dependency is called out in its heading, and the enable toggle is labeled "Enabled" consistently. Schema-metadata + UI-rendering only — no processing-logic change.

- **Evaluation reports show the per-field comparison Method and Weight for nested list/object fields.** Each item field of an array-of-objects (e.g. `LineItems[].Amount`) now displays its own comparator Method and Weight in the Nested Field Comparison table and the Visual Editor overlay, consistent with the top-level attributes table. These are re-derived on read from the translated schema (Stickler drops them from its output), so no re-run is needed. See the [evaluation guide](docs/evaluation.md).

- **GovCloud Deployment guide restructured around the two `idp-cli` paths (`--govcloud` with the Web UI vs. `--headless`).** `docs/govcloud-deployment.md` now opens with a decision table and copy-paste `idp-cli deploy` commands for each variant, corrects a stale claim that `--headless` auto-sets `EnableHeadless=true`, and links to the rewritten GovCloud Architecture page (which documents both variants and corrects the "Pattern 2 only" claim — BDA mode is available in `us-gov-west-1`).

### Fixed

- **Failed and timed-out Step Functions steps were shown as "Running" in the execution view.** The step-history parser's failure handlers (`TaskFailed` / `TaskTimedOut` / `LambdaFunctionFailed`) looked up the affected step by its display *name*, but the correlation helper returns a step *key* (`<name>_<eventId>`); the two never matched, so a failed step was never transitioned out of `RUNNING`. The web UI's execution detail therefore rendered a genuinely-failed step as still running. The handlers now match on key-or-name. (`get_stepfunction_execution_resolver`.)

- **`make test` now runs every test suite (auto-discovered), and the test-runner won't let new suites be silently skipped.** `make test` was a hand-maintained directory list, so ~200 tests across ~22 Lambda/resolver dirs were never run by it. A single root-level `pytest` can't replace it (the many `tests/conftest.py` files collide), so `scripts/run_all_tests.py` runs each test root as an isolated invocation but **discovers** the roots and fails if any discovered directory isn't explicitly registered as run-or-quarantined — closing the gap that let new tests go unrun. New targets: `make test`, `make test-integration-all`, `make test-list`. Also fixed `save_reporting_data`'s tests (they reached real AWS and asserted a stale call signature).

- **`idp_common` unit gate now runs the full unit suite (~810 more tests) instead of only `@pytest.mark.unit`-tagged ones.** `lib/idp_common_pkg`'s `test-unit`/`test-cicd` targets filtered on `-m "unit"`, but hundreds of real unit tests were never tagged with that marker, so CI silently collected only ~1,564 of ~2,374 tests. The filter is now `-m "not integration"` (matching `pytest.ini`'s default and the rest of the repo), and the 28 tests that had rotted while unrun are fixed so the wider suite is green: obsolete `sync_custom_with_new_default` auto-merge assertions rewritten to the current no-op snapshot contract; stale default-model/compression assertions in the discovery embedding/visual-tool tests updated (and made to use real images); the "missing assessment section" test moved to the v0.6 `extraction.confidence.task_prompt` location; `test_publish` environment tests de-mocked of a since-removed private method; a flaky concurrent-batch embedding test made order-independent (its mock keyed on input rather than call order); and two mislabeled discovery integration tests marked `@pytest.mark.integration` like their siblings. Two module-level `sys.modules` mocks (`pypdfium2` in the OCR test, `PIL` in the assessment test) were leaking globally and corrupting later test files that build real PDFs/images — they now only fall back to a `MagicMock` when the real module is genuinely absent.

- **Reasoning models (Claude Sonnet 5, Sonnet/Opus 4.6+, any model with extended thinking) returned empty extractions/classifications.** These models emit `reasoningContent` blocks before the answer `text` block, but the parsers read only `content[0].get("text")` — the reasoning block — yielding an empty string and silent 0% accuracy. The parsers now concatenate all `text` blocks and skip `reasoningContent`. Affected extraction, classification, assessment, and summarization.

- **Config-version scope was silently unenforced for configuration reads (fail-open).** The `ConfigurationResolverFunction`'s IAM policy granted DynamoDB access only to the ConfigurationTable, not the UsersTable, so every `allowedConfigVersions` lookup failed with `AccessDeniedException` — which the resolver treats as "no restriction", silently granting a config-version-scoped non-admin user unrestricted access to every configuration version. The role now grants `Query`/`GetItem` on the UsersTable and its indexes. Found by the new API RBAC test harness (`make api-test`).

- **Reviewers can save section edits again in the Visual Editor (HITL review).** Saving as a Reviewer failed with `Not Authorized to access uploadDocument` because the save path requested a presigned upload URL via a mutation restricted to Admin/Author. The reviewer save now routes through the reviewer-permitted `completeSectionReview` mutation, which writes the edited JSON server-side; the resolver raises instead of silently succeeding when the output URI can't be resolved. Admin/Author keep the presigned path.

- **Confidence assessment no longer silently loses scores + bounding boxes when the model truncates its output.** A large per-row assessment batch could exceed a small-cap confidence model's max output tokens (e.g. Nova Lite's 10,000), yielding unparseable JSON that was silently replaced with a default `0.5` and null placeholders. The core now detects truncation (`stopReason == "max_tokens"`, plus the OpenAI-Responses `incomplete` status) and the large-list batcher recursively halves any truncated slice and re-assesses it until it fits, surfacing split activity in `metadata.assessment_batch_split_stats`.

- **Documents with `#` in their filename no longer fail with `NoSuchKey` at every processing stage.** `urlparse` treats `#` as a URL fragment delimiter and silently truncated S3 keys in `Document.decompress()`, `S3Util.s3_url_to_bucket_key()`, and the reporting data writer. All three now use `parse_s3_uri()`, which splits the URI literally and preserves any key character. (#477)

- **"View Data" / "Download" on large section results no longer fails with "Failed to load content".** The section-data viewer fetched each `result.json` inline through a resolver, so a large section (e.g. a ~5.7 MB result) pushed the response past Lambda's hard 6 MB synchronous payload limit and the invocation threw. A new `getFilePresignedUrl` query returns a short-lived SigV4 presigned GET URL so the browser fetches the bytes directly from S3 — no size limit. (#458)

- **"Use as Evaluation Baseline" now actually updates the document's Evaluation status.** The copy-to-baseline resolver had code to write `EvaluationStatus` but its IAM role was missing DynamoDB write permission, so the S3 copy succeeded while every status write failed with `AccessDeniedException` (caught and swallowed), leaving the UI stuck on `NO_BASELINE`. Added a `DynamoDBCrudPolicy` on the tracking table; the document detail view now also passes `evaluationStatus` so the baseline badges appear.

- **Evaluation no longer fails a whole document when a `required` field is correctly null.** A config marking a field `required` that a document genuinely lacked raised `Field required [type=missing]` after null-stripping, zero-scoring every attribute in the section. Evaluation now clears `required` arrays (matching the auto-generated-schema path), so a missing field scores as a normal miss on that one attribute.

- **Advanced (agentic) extraction cost is reported correctly.** The Assessment step's "intelligent skip" discarded the Extraction step's Bedrock/lambda metering, making advanced modes look nearly free in cost reports; the incoming metering is now preserved.

- **Extraction prompts respect per-field date/number formats from the Document Schema** (previously a hardcoded MM/DD/YYYY instruction overrode per-field formats), and **every list/nested-list cell gets its own confidence + bounding box** (previously a single score could apply to a whole row).

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.6.0.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.6.0.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.6.0.yaml`
  
## [0.5.16]

### Added

- **Consolidated OCR page data (`pageData.json`) — text + confidence + geometry in one backend-agnostic artifact** — `OcrService` now writes a per-page `pageData.json` consolidating text, confidence, and geometry into a single LINE-primary schema (optional WORD children), where `confidence` and `geometry` are independently optional so each backend contributes what it has (Textract: per-LINE/WORD box + polygon; Mistral hook: per-LINE confidence with paragraph-level boxes; Chandra/plain-LLM: text-only). Geometry is normalized 0–1; the artifact is derived in-process (no extra OCR calls) and surfaced via `Page.ocr_page_data_uri`. The Web UI Page viewer gains a **Visual Editor** view (now the default) — page image plus OCR lines, click a line to draw only its bounding box (color-coded confidence), with zoom/pan and full-document page navigation; it degrades to image-only when a backend has no geometry. Fully additive and backward-compatible: existing files and the `{OCR_TEXT_CONFIDENCE}` prompt are unchanged (zero token impact) and older documents simply lack the file. See [the OCR README](lib/idp_common_pkg/idp_common/ocr/README.md#consolidated-ocr-page-data-pagedatajson).

- **Mistral OCR (serverless) LambdaHook + structured confidence/geometry capture** — New sample hook `samples/lambda-hook-inference/GENAIIDP-mistral-ocr-hook/` integrates the hosted [Mistral OCR](https://mistral.ai/news/ocr-4/) API as an OCR backend via the existing LambdaHook feature — fully serverless, no SageMaker/GPU. It requests structured output and translates the response into Amazon Textract format (per-LINE confidence averaged from word scores, WORD blocks, normalized 0–1 geometry) under a new `textractBlocks` key. `OcrService` now detects `textractBlocks` on any LambdaHook OCR response and persists it as `rawText.json` + a real `textConfidence.json` (instead of the placeholder), so OCR confidence flows into Assessment and geometry into UI highlighting — exactly like native Textract. Text-only hooks (e.g. Chandra) are unaffected. Reports `usage.pages` for cost metering; `pricing.yaml` gains a `GENAIIDP-mistral-ocr-hook` entry ($0.004/page). See [docs/lambda-hook-inference.md](docs/lambda-hook-inference.md).

- **Web UI cost view: pricing match for LambdaHook (and other ARN-keyed) metering** — The Estimated Cost table now resolves unit prices with exact-then-partial matching (mirroring the backend's `_get_unit_cost`), so a generic pricing entry (e.g. `GENAIIDP-mistral-ocr-hook`) matches a metering key that embeds the full Lambda ARN. Previously the exact-match-only lookup showed `Unit Cost: None` / `N/A` for all LambdaHook usage.

- **Assessment bounding boxes grounded in real OCR geometry (`pageData.json`)** — The Assessment service now runs a post-LLM enrichment pass that matches each extracted value against the real OCR lines in `pageData.json` and, on a confident match (tiered exact → fuzzy ≥ 0.6, with spatial disambiguation when a value repeats across rows), replaces the LLM-estimated bounding box with the real OCR box. The swapped `geometry` keeps the same shape (0–1, 1-indexed page — no UI change) and adds `geometry_source` and `ocr_confidence`; the LLM `confidence`/`confidence_reason` are never touched, so HITL thresholds are unaffected. Covers standard and granular assessment via `idp_common.assessment.ocr_grounding`, gated by `assessment.ground_geometry_in_ocr` (**default on**), with the token budget unchanged. Safe fallback to prior LLM-only behavior when `pageData.json` is absent, a backend provides no geometry, or no match is found. See [docs/assessment-bounding-boxes.md](docs/assessment-bounding-boxes.md) and [the assessment README](lib/idp_common_pkg/idp_common/assessment/README.md).

- **Agentic extraction: full-schema validation + model escalation (Phase 1)** — Agentic extraction can validate its output against the complete class JSON Schema (including `format` keywords the generated Pydantic model doesn't enforce — `date`, `email`, `uri`, ...), collect all violations at once with readable field paths, and on failure escalate to a stronger model. New `extraction.agentic.validation` block: `enabled` (default **false** — no behavior change on upgrade), `check_formats` (default true), `fail_action` (`warn` | `escalate` | `reject`, default `escalate`), and `escalation_model`. On `escalate`, only the failing top-level fields are re-extracted (per-class override via `x-aws-idp-extraction-escalation-model`) and merged back, kept only if valid or with strictly fewer errors. Each section's `metadata` records the model used and the validation/escalation details. Editable in the Configuration UI. Note: `format: date` is ISO-8601, so the default `MM/DD/YYYY` prompt fails format checks — set `check_formats: false` or use `pattern` for non-ISO dates. See [the extraction README](lib/idp_common_pkg/idp_common/extraction/README.md) and `notebooks/misc/agentic-extraction-validation-and-escalation.ipynb`.

- **Per-class extraction prompt overrides (#377)** — Classes can override the global extraction prompts via two new schema extensions, `x-aws-idp-extraction-system-prompt` and `x-aws-idp-extraction-task-prompt` (absent → global prompts; fully backward compatible). The task override supports the same placeholders as the global one and composes with a configured custom-prompt Lambda (used as defaults; the Lambda still wins). This mirrors the existing per-class `x-aws-idp-extraction-model` override, letting independently-tuned single-class configs keep their prompts when merged into a multi-class config. Editable per-class in the Configuration UI. See [docs/extraction.md](docs/extraction.md).

- **Classification valid-class enforcement (#356)** — Page-level classification (`multimodalPageLevelClassification`) now validates the predicted class against the configured vocabulary and, on an out-of-vocabulary prediction, re-prompts the model with the allowed classes (up to a retry limit); exhausted retries assign a fallback class and flag a `validation_error` (no hard failure). New keys: `enforceValidClasses` (default `true`), `maxValidationRetries` (default `2`), `invalidClassFallback` (default `unclassified`), all editable in the UI. **Behavior change on upgrade:** enforcement is on by default — set `enforceValidClasses: false` to restore the prior "use as-is" behavior. Holistic packet classification is not yet covered. See [docs/classification.md](docs/classification.md) and `notebooks/misc/classification-valid-class-enforcement.ipynb`.

- **Processing Report surfaces extraction model, validation, and population metadata** — The section Visual Editor's **Processing Report** tab now renders the agentic schema-validation/escalation and completeness-heuristic metadata it previously ignored: the extraction **model** used (noting per-class overrides), a **Schema Validation & Escalation** panel (validation result, `fail_action`, escalation outcome and before→after error counts, expandable violations), and a **Field Population** panel (completeness ratio, threshold, empty fields). The "Extraction Issues Detected" banner now also fires on a failed validation or below-threshold population, so silent loss is visible at a glance. Purely additive (reads existing `metadata.*`); older sections render unchanged.

- **Per-job `configurationVersion` on the headless Jobs API (#387)** — `POST /jobs` accepts an optional `configurationVersion` to pin a specific config version per submission (enables A/B testing and gradual config migrations). The value is validated (≤128 chars, `^[a-zA-Z0-9._-]+$`; invalid → 422), persisted on the job record, and re-applied as `config-version` S3 metadata on each extracted file; `GET /jobs/{id}` echoes it back. Optional and defaults to `null` (identical to prior behavior); an unknown version falls back to pipeline mode. See [docs/govcloud-batch-api.md](docs/govcloud-batch-api.md).

- **Configurable Lambda architecture** — New `LambdaArchitecture` parameter (`arm64` or `x86_64`) for all unified-pattern Lambda container images, defaulting to `arm64` (Graviton) for best price-performance. Use `x86_64` when deploying with custom base images that only support AMD64. Flows through to CodeBuild (`--platform`) and the Dockerfile `FROM` suffix.

### Removed

- **Global split panel ("documents selected")** — Removed the persistent bottom split panel from the global Web UI layout (and the related components and `use-split-panel` hook). It was noisy on non-document-list pages and only provided full details for single-document selection; reintroduce as an opt-in, per-page component if needed.

### Fixed

- **Agentic extraction: `max_concurrent_batches` now actually shards the input (fixes context-window overflow on long sections)** — Concurrent batch extraction previously handed every batch agent the *whole* document and merely instructed it (in text) to ignore other pages, so raising `max_concurrent_batches` gave no context relief and multiplied token cost ~N×; long, dense sections overflowed the context window, surfacing as the misleading Strands *"insufficient messages for summarization"* error. It now shards the section into token-budgeted page ranges (`extraction.agentic.shard_token_budget`, default 8,000) bounded also by a page ceiling (`extraction.agentic.max_pages_per_shard`, default 5), each agent seeing only its pages. Splits are page-aligned (no table-row loss), page-1 text propagates to later shards as header context, and scalar fields merge first-non-null with disagreements recorded in `metadata.shard_scalar_conflicts`. Single-pass extraction (`max_concurrent_batches: 1`, the default) is unchanged. See [the extraction README](lib/idp_common_pkg/idp_common/extraction/README.md).

- **Agentic extraction: clear error on context-window overflow** — When an input exceeds the model's context window, the agent loop now raises an actionable message (enable concurrent sharding / lower `shard_token_budget`, enable table parsing, reduce attached page images, or use a `:1m` model) instead of the opaque *"insufficient messages for summarization"* error.

- **Agentic extraction silently dropped nested-object field values** — Class schemas with nested object (or array-of-object) properties whose names contain spaces (e.g. `"Date of Birth"`) returned `null` for nearly every nested field. `create_pydantic_model_from_json_schema` applied the alias round-trip config (`populate_by_name` / `serialize_by_alias` / `validate_by_*`) only to the top-level model, so nested sub-models dropped or mis-keyed aliased values; the config is now propagated recursively to every nested model. This also aligns nested keys with how Evaluation and Assessment look them up. PascalCase/underscore-free schemas were unaffected.

- **Agentic extraction completeness heuristic (silent-loss detection)** — After extraction, an advisory check computes the fraction of schema-defined leaf fields that came back populated and, below `extraction.agentic.validation.min_population_ratio` (default `0.5`; set `0` to disable), logs a warning and flags `metadata.population_check` (`fields_defined`, `fields_populated`, `population_ratio`, `below_threshold`, `empty_fields`). This surfaces silent loss — the nested-field bug above, or a table that extracted zero rows — that schema validation alone can't catch. Advisory only: it never fails extraction.

- **Document Data panel: no confidence shown for fields the assessment decomposed into sub-keys** — When assessment split a plain-string field (e.g. `Insurance Company`) into sub-keyed child assessments, the field had no top-level `confidence` and showed none. `getFieldConfidenceInfo` now aggregates the children using the **minimum** child confidence (worst-case signal for review) and that child's threshold. Relatedly, OCR geometry grounding now grounds such decomposed fields by matching each sub-key to its OCR line, so they get real bounding boxes on the correct page.

- **Page Viewer: selecting an OCR line jumped back to page 1** — In the Page viewer's Visual Editor, clicking a line to highlight its box reset multi-page documents to page 1 because the geometry was tagged `page: 1` (hardcoded); it now carries the page being viewed. Page navigation is also exposed as prominent **Previous Page / Next Page** footer buttons in addition to the image arrows.

- **Web UI blocked `.txt` uploads despite backend support (#373)** — The file-picker allow-list (`SUPPORTED_UPLOAD_EXTENSIONS`) omitted `.txt` even though OCR already processes plain text, forcing users to upload directly to S3. Added `.txt` to the allow-list and the supported-formats label. Discovery upload remains intentionally PDF/image-only.

- **`idp-feature-cli deploy-pack` was create-only (#383)** — `deploy_pack()` called `create_stack(..., OnFailure="DELETE")` unconditionally, so re-running it against an existing wrapper stack failed with `AlreadyExistsException` (forcing a full delete/recreate, ~20 min). It now delegates to `create_or_update_stack()`: creates if absent, updates in place if present, treats a no-op update as success, and refuses to update a `ROLLBACK_COMPLETE`/`ROLLBACK_FAILED` stack with a clear "delete it first" message. The wrapper's host custom resource treats `Update` as a no-op so the running host is undisturbed. See [the SDK README](lib/idp_feature_sdk/README.md).

### Changed

- **Extraction config UX: progressive disclosure + surfaced hidden options** — The agentic-extraction config in View/Edit Config now uses `dependsOn` to progressively disclose options (agentic sub-options only when agentic is on, table-parsing thresholds only when the parser is on, validation sub-fields only when validation is on, escalation model only when `fail_action: escalate`), with clarified labels and help text. Three previously YAML-only options are now surfaced: `table_parsing.max_empty_line_gap`, `table_parsing.auto_merge_adjacent_tables`, and the `missing_field_handling` block. Presentation-only — no keys renamed or removed; stored configs and evaluation baselines load unchanged. See [the extraction README](lib/idp_common_pkg/idp_common/extraction/README.md).

- **Web UI CloudFront origin access: OAI → OAC (#369)** — The CloudFront-hosted Web UI now reads its S3 origin via **Origin Access Control (OAC)** instead of the legacy Origin Access Identity; `WebUIBucketPolicy` grants `s3:GetObject` to the `cloudfront.amazonaws.com` principal scoped by an `AWS:SourceArn` condition. This fixes a **403 / Access Denied** loading the UI in accounts whose org SCP or data-perimeter guardrails block legacy OAI requests. Upgrade is in-place and non-disruptive (same domain name); gated on `WebUIHosting=CloudFront`, so GovCloud and ALB deployments are unchanged.

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.5.16.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.5.16.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.5.16.yaml`
  
## [0.5.15]

### Added

- **Feature Platform — installable features ("marketplace of extensions")** — The main stack can now host *installable features*: independent CloudFormation stacks that an admin launches into the same account, which upload their UI bundle into the host's `WebUIBucket` and register themselves in a new `InstalledFeatures` DynamoDB table. Registered features appear as nav items in an "Extensions" section of the existing web UI, each rendering its own UMD-loaded React bundle via a shared host-globals contract (`window.IdpFeatures.register`). Backed by a nested `FeaturePlatformStack` (under `feature-platform/main-stack-extensions/`) exposing AppSync operations for catalog listing, entitlement, install/uninstall, and registration. Two feature kinds are distinguished by a `source` field: **OSS features** (bundled, open-source — installed via a plain CloudFormation quick-create URL) and **Marketplace features** (closed-source, *future* — handed out as a presigned URL only after `GetEntitlements` confirms an AWS Marketplace subscription; none ship today). Discovery is **manifest-driven**: `idp-cli publish` writes a single `catalog.json` into the stack's own ConfigurationBucket, read at runtime with one `GetObject` (no `ListObjectsV2`; the deployed stack does not depend on the artifacts bucket for the catalog). **On by default** (`EnableFeaturePlatform=true`) in **auto-subscribe** mode (every catalog feature treated as subscribed, UI goes straight to Install); set `FeaturePlatformSimulatorEndpoint` to attach a marketplace-simulator or real Marketplace endpoint, or `EnableFeaturePlatform=false` to remove the platform entirely. Features can also ship a **config preset** — applied at install as a new, non-active config version (`<featureId>-v<version>`) for an admin to activate, so installation never changes the active configuration — via IAM-auth AppSync mutations `applyFeatureConfigPreset` / `removeFeatureConfigPreset`. The main stack re-exports `OutputBucketName`, `WorkingBucketName`, and `DiscoveryBucketName` for features that read processing results or drive the host's Rules Discovery flow. See the new [Feature Platform](docs/feature-platform.md) and [Developer Guide](docs/feature-platform-developer-guide.md).
  - **Bundled OSS samples** — **Sample: Health Insurance Review** (`feature-platform/sample-health-insurance-review/`) demonstrates a health-insurance-claims vertical end-to-end: it applies a prior-auth rule-validation config preset, registers a `postRuleValidation` pipeline hook that derives a deterministic claim status (Clean Claim / Review Required / Insufficient Documentation) into its own DynamoDB table, exposes a multi-route Cognito-auth feature API, and renders a two-tab UI (Claims Dashboard with per-rule drill-down + a Rules Discovery tab driving the host's discovery operations via the shared Amplify instance). The minimal **Sample: Document Status** (`docs-by-status`) remains the contract reference. See [Sample: Health Insurance Review](docs/extensions/sample-health-insurance-review.md).
  - **`idp-feature-cli`** — authors scaffold features with `init`, publish artifacts with `publish`, and use `deploy` (the per-extension analogue of `idp-cli deploy`) to build, publish, and create-or-update one feature's stack against a running host for fast inner-loop iteration. Requires the AWS SAM CLI. See the [Developer Guide](docs/feature-platform-developer-guide.md).

- **Pipeline hooks — extension points in the unified workflow** — The unified Step Functions workflow now invokes a `PipelineHooksDispatcherFunction` at six post-step extension points (`postOcr`, `postClassification`, `postExtraction`, `postAssessment`, `postRuleValidation`, `postSummarization`). The dispatcher reads the active configuration version's `<step>.postHook` list from the ConfigurationTable and fans out to feature-registered hook Lambdas in `order`, passing the document/section payload. Hooks are **inert by default**: with no `postHook` config the dispatcher returns after a single DDB read and the pipeline is unchanged. Hook Lambdas must either carry the `idp:feature-id` resource tag (ABAC for vertical-product packs) or follow the `GENAIIDP-*` naming convention — anything else fails closed with `AccessDenied`. Each SFN hook state is catch-all so a missing or erroring hook never breaks the pipeline; per-hook `onError` policy (`continue`/`skip-remaining`/`fail`) is configurable. This is the foundational extension point for the Feature Platform (installable subscription features) but is independently useful for injecting custom business logic mid-pipeline.
  
- **Custom domain support for ALB-hosted private deployments** — new optional `CustomDomainUrl` stack parameter (under the *ALB Hosting* parameter group) lets the Web UI be reached through a customer-owned DNS alias, an Okta/SAML/OIDC SSO domain, or any other origin in front of the ALB **without losing access via the original ALB URL**. When set:
  - the custom origin is added to the `AllowedOrigins` of all seven browser-accessed S3 buckets (`InputBucket`, `OutputBucket`, `ConfigurationBucket`, `ReportingBucket`, `EvaluationBaselineBucket`, `TestSetBucket`, and `DiscoveryBucket`) so direct presigned-URL uploads/downloads succeed;
  - the custom origin (with and without trailing slash) is added to both Cognito App Client `CallbackURLs` and `LogoutURLs` (`ExternalAppClient` + the main `UserPoolClient`) so OAuth redirects — including federated Okta/SAML/OIDC sign-in via the Cognito Hosted UI — land back on the custom domain;
  - `VITE_CLOUDFRONT_DOMAIN` is set to `""` at Web UI build time so `aws-exports.js` falls back to `window.location.origin` for `redirectSignIn` / `redirectSignOut`. The same build serves the ALB URL **and** the custom domain — Amplify always starts and finishes the OAuth flow on the same origin, removing the *"redirect is coming from a different origin"* error seen with custom DNS in front of ALB.
  
  Validated by `AllowedPattern` at template-parse time (lowercase-host `https://` URL, no path, no trailing slash). Default `""` resolves every conditional to `AWS::NoValue` so the rendered template is byte-identical to before for existing deployments — fully backward compatible. **Customer-side requirements** (outside the template): point the custom-domain DNS at the internal ALB, attach an ACM cert that covers the custom domain to the ALB listener (or use SNI), and — for federated IdPs — confirm Cognito's `oauth2/idpresponse` URL is registered as a redirect URI in the IdP. No Okta-side changes are needed for the custom domain itself; Okta only ever sees the Cognito `idpresponse` URL in this flow. See [`docs/alb-hosting.md`](docs/alb-hosting.md#custom-domain-in-front-of-alb).

- **AppSync DNS resolution documentation for cross-VPC/hybrid networks** — comprehensive guide in `docs/deployment-private-network.md` explaining why AppSync private APIs require DNS forwarding in central-network-account topologies, with step-by-step instructions for Route 53 Private Hosted Zone setup (same-account, cross-account, and hybrid/on-prem) and an alternative ALB reverse-proxy architecture for environments where DNS changes are not possible. New `AppSyncEndpointForDNS` stack output provides the hostname networking teams need for PHZ alias records.

- **CodeBuild VPC support** — All CodeBuild projects (WebUI build, unified Docker build, multi-doc-discovery Docker build, SDLC pipeline) now run inside the customer's VPC when `DeployInVPC=true`. Build traffic routes through the VPC, reaching public registries via NAT gateway or internal artifact repositories in air-gapped environments. This is a prerequisite for fully private deployments where builds must pull dependencies from internal registries. No changes when `DeployInVPC=false` (the default).

### Fixed

- **Test Studio results error for runs stuck in evaluation (#358)** — `getTestRun` (the `test_results_resolver` Lambda) raised an unhandled `ValueError` ("Test run … processing completed, evaluating results") when a run reached a terminal state but the evaluation-aggregation step never cached `testRunResult` — e.g. when aggregation is still running, timed out, or failed silently on a large run (the reporter hit this with 3 463 documents). The exception surfaced as an opaque error and the run spun on "Loading…" forever in Test Studio. The resolver now returns a structured partial `TestRun` (true status plus file counts and metadata, metric fields omitted) instead of raising, so the UI renders the in-progress/terminal state gracefully. This also stops a single not-yet-aggregated run from failing an entire `compareTestRuns` request. (The separate question of *why* aggregation can stall on very large runs is tracked as a follow-up.)
- **Configuration version list silently truncated past the first page (#354)** — `ConfigurationManager.list_config_versions()` performed a single unpaginated `table.scan()` on the ConfigurationTable. Because a DynamoDB scan returns at most 1 MB per call, deployments with many config versions (e.g. 230+) only ever saw the ~58 that fit on the first page — uploaded-via-CLI and autotune-agent configs were invisible in the UI's View/Edit Configuration page and the upload-document config-version dropdown (the configs still worked when referenced by name). The method now paginates through `LastEvaluatedKey` so every version is returned. Fixes all callers (`update_configuration`, the AppSync `configuration_resolver`, `rules_discovery`, and the SDK).

- **Build Info "update available" indicator broke against the public release bucket** — The `getLatestPublishedVersion` resolver discovered the newest published version by calling `ListObjectsV2` on the public artifacts bucket and parsing `idp-main_<version>.yaml` keys. That bucket grants `GetObject` only (no listing), so the check failed on real public deployments. `idp-cli publish` now writes a small pointer object — `<prefix>/idp-main-latest.json` (`{version, templateUrl}`) — at the version-stripped prefix on every release, and the resolver reads that one known key with a single `GetObject` (unsigned, falling back to signed), with a conventional `idp-main_<version>.yaml` URL fallback if the pointer omits one. No version parsing or `ListObjectsV2`. The check stays disabled when `PUBLIC_ARTIFACTS_BUCKET` is unset.

- **Private AppSync unreachable from browser clients (WorkSpaces, VPN, bastion)** — `scripts/vpc-endpoints.yaml` `VpcEndpointSecurityGroup` previously allowed inbound HTTPS (port 443) only from the Lambda security group. Browsers inside the VPC send AppSync GraphQL requests directly to the `appsync-api` VPC Interface Endpoint (not through the ALB), so all queries, mutations, and subscriptions hung indefinitely — the Configuration page showed "Loading configuration..." forever, the Document List never populated, and the Upload Documents page showed "Input bucket not configured". Fixed by adding a `VpcCidr` parameter and a second ingress rule for the VPC CIDR block. `deploy-vpc-endpoints.py` now auto-looks up the VPC primary CIDR via `ec2:DescribeVpcs` and passes it automatically — no CLI changes required. Re-run `deploy-vpc-endpoints.py` against an existing deployment to apply the fix.

### Changed

- ⚠️ **Behavioral change for air-gapped ALB deployments: browser S3 presigned uploads now default to global S3 instead of the S3 VPC Interface Endpoint.** Previously, selecting `WebUIHosting=ALB` automatically forced all presigner Lambdas to generate presigned URLs targeting the S3 VPCE hostname (`*.vpce-xxx.s3.<region>.vpce.amazonaws.com`), which required the browser's corporate network to resolve and route VPCE DNS — a configuration many customers can't or don't want to set up (uploads failed with `NS_Net_Timeout`). Presigned-URL routing is now decoupled from ALB hosting via a new `S3PresignedUrlViaVpcEndpoint` parameter (default `"false"`): presigned URLs use global `s3.amazonaws.com`, so browser uploads transit NAT/internet with no special DNS needed. **Migration:** existing `WebUIHosting=ALB` stacks that rely on VPCE presigned uploads (fully air-gapped browser networks with no NAT path) must set `S3PresignedUrlViaVpcEndpoint=true` on their next stack update, otherwise browser uploads switch to global S3. Stacks setting `S3VpcEndpointIdOverride` (BYO endpoint) are unaffected — they continue to use the VPCE. A CloudFormation `Rules` assertion rejects `S3PresignedUrlViaVpcEndpoint=true` unless an S3 VPCE is available (`WebUIHosting=ALB` or `S3VpcEndpointIdOverride` set).

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.5.15.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.5.15.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.5.15.yaml`

## [0.5.14]

### Added

- **Dependency manifest generation for artifact-repository mirroring** — New `make dep-manifest` target (and `scripts/generate-dep-manifest.sh`) generates a complete, pinned list of all Python and Node.js dependencies for enterprises mirroring packages into an artifact repository (JFrog Artifactory, AWS CodeArtifact, Sonatype Nexus, etc.) for air-gapped, pre-scanned builds. Parses existing `uv.lock` and `package-lock.json` files (no re-resolution) plus any extra `requirements.txt`/`pyproject.toml` packages, writing pip-compatible (`name==version`) and npm-compatible (`name@version`) manifests to the gitignored `dist/manifests/`. A GitHub Actions workflow (`generate-dep-manifest.yml`) regenerates manifests on dependency-file changes (dry-run on PRs, 90-day artifact upload on `main`/manual dispatch). See the new [Dependency Mirroring](docs/dependency-mirroring.md) guide.

- **OpenAI GPT-5.4 / GPT-5.5 model support** — Added `openai.gpt-5.4` and `openai.gpt-5.5` everywhere Claude models are selectable (OCR, classification, extraction, assessment, summarization, evaluation, and Chat-with-Document). Unlike all other supported models, these are served via the **`bedrock-mantle` endpoint (OpenAI Responses API)** rather than the Converse API, so a new SigV4-signed HTTP backend was added in `idp_common/bedrock/openai_responses.py`. `BedrockClient.invoke_model` transparently routes `openai.gpt-5.*` IDs to it and returns the identical `{"response", "metering"}` contract, so no downstream service code changed. The Chat-with-Document processor routes GPT-5.x through a streaming Responses call (SSE), publishing incremental token deltas to the UI with the same throttling as the Converse path. Notes: these are reasoning models (temperature/top_p/top_k omitted) tuned via a new OpenAI-only `reasoning_effort` config field (`minimal`/`low`/`medium`/`high`, default `medium`) surfaced per service in the unified template and config models; **no prompt caching** (`<<CACHEPOINT>>` stripped); **incompatible with agentic/Strands extraction** — that combination is now a hard error in `idp-cli config-validate` and raises at runtime; **not supported for Discovery** (which ingests whole-PDF document blocks the Responses API can't accept) — rejected by config-validate and guarded at runtime; **standard service tier only**. Availability is **US regions only** — GPT-5.5 in `us-east-2`; GPT-5.4 in `us-east-2`, `us-west-2`, `us-gov-west-1` (no EU/global), so the models are hidden in EU-region deployments. Lambda roles gained `bedrock-mantle:CreateInference` (+ `Get*`/`List*`) IAM permissions. New env vars: `BEDROCK_MANTLE_REGION` (pin the mantle region), `BEDROCK_MANTLE_SIGNING_NAME`, `BEDROCK_MANTLE_REASONING_EFFORT`. Includes pricing and `model_config_limits` (128K max output) entries. See the new [OpenAI GPT-5.x Models](docs/openai-models.md) guide for the full support matrix and caveats.

- **Test Studio: Edit test set metadata** — Test sets can now be edited to update description (max 500 characters) and document classification type. Edit functionality available via new "Edit" button when a single test set is selected. Classification type metadata options: Unspecified, Single Class, Multi Class, Packet Splitting. Includes new `UpdateTestSetInput` GraphQL input type, `updateTestSet` mutation with `UpdateTestSetResolver` AppSync resolver, Lambda handler routing, and frontend Edit Test Set modal with form validation.

### Fixed

- **Agentic extraction no longer crashes merging token metering with `None` values** ([#337](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/337)) — `concurrent_structured_output_async` (used when a large document is split into concurrent extraction batches) raised `TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'` when a Bedrock response reported a token counter as `None`. The existing `(tv or 0)` guard only covered the incoming value; the accumulated value (seeded verbatim from the first batch via `dict(mv)`) could itself be `None`, and `dict.get(tk, 0)` returns that stored `None` rather than the default for a present-but-`None` key. The metering merge is now factored into `_accumulate_metering`, which coerces `None` on both sides of the addition. This previously crashed otherwise-successful extractions in the post-processing step, marking the document FAILED.

- **Evaluation no longer fails on `None`/empty optional fields, empty arrays, or a single bad field** — Three related evaluation robustness fixes: (1) Optional fields with `None`/missing values (common in real schemas like URLA) no longer fail the confidence/assessment path with a misleading "Schema configuration error" — model fields are now widened to `Optional[...]` to work around upstream [stickler#149](https://github.com/awslabs/stickler/issues/149). (2) Auto-generated schemas with empty arrays (e.g. `[]` → bare `{"type": "array"}`) and objects that become empty after their unevaluable children are removed are now pruned instead of crashing the converter; genson's spurious `required` arrays are also stripped so a missing field scores as a miss rather than a hard error. (3) A single field that still fails validation is now dropped from scoring (and reported as a `__SKIPPED__` row with a coverage note) instead of zeroing out the entire section — limiting the blast radius so the remaining fields still evaluate.

- **Schema Builder: Standard Class catalog restored in "Add Class" modal** — The Document Schema *Add Class* modal again presents the two-card chooser (📝 Custom Class / 📦 Standard Class) for non-policy schemas, letting users import pre-built classes from the [Standard Class Catalog](docs/classification.md#standard-document-classes) (Invoice, Receipt, US driver's license, etc.). The chooser/standard-mode UI was inadvertently dropped from `SchemaBuilder.tsx` during the policy-discovery rewrite (commit `d701e6b88`); the underlying `StandardClassCatalog` component, `addStandardClasses` hook action, and `standard-classes.json` data file were all still present and needed only to be re-wired into the modal. Policy Schema "Add Policy Class" still skips the chooser and goes straight to the custom form (unchanged behavior).

- **Evaluation now handles null field descriptions** — Configs with `description: null` no longer cause evaluation failures. The evaluation service now automatically converts null descriptions to empty strings before JSON Schema validation (Stickler requirement). This fix ensures extraction results can be evaluated even when field descriptions are missing or null in config schemas. No functional impact on evaluation logic.

- **Updated the AppSync APIs** (1) all field-level `@aws_auth(cognito_groups:[…])` directives in `schema.graphql` replaced with `@aws_cognito_user_pools(cognito_groups:[…])`, which AppSync evaluates on multi-auth APIs; (2) server-side Cognito group checks added to every privileged resolver Lambda.

- **Navigation cleanup** — removed the "Resources" dropdown (Blog, Code) from the top-right user menu and added a "Blog" link to the top of the Resources section in the left navigation panel.

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.5.14.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.5.14.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.5.14.yaml`

## [0.5.13]

### Added

- **Claude Opus 4.8 Model Support** — Added `anthropic.claude-opus-4-8` (and `:1m` context variant) across all `us`, `eu`, and `global` inference profiles. Includes unified template enums, UI model dropdowns, cachepoint support, EU region mappings, pricing entries, and documentation updates. Model is recognized as a Claude 4.7+ variant — the same temperature/top_p/top_k handling and 128K extended-output limit apply.

- **Amazon Quick + IDP MCP Integration Workshop** — step-by-step guide (`workshop/amazon-quick-integration-workshop.md`) that walks through deploying the IDP stack, configuring MCP connectivity in Amazon Quick, and building an end-to-end loan document processing workflow with structured data extraction and Excel output. Covers CloudFormation deployment, OAuth service-to-service auth setup, action configuration, and a multi-phase Amazon Quick workflow.
  
- **Stickler v0.4.0 upgrade with confidence calibration metrics** — upgraded evaluation engine from Stickler v0.1.4/v0.1.5 to v0.4.0, adding ECE (Expected Calibration Error), Brier score, ECARB@30, and AUROC metrics for confidence analysis. New `ConfidenceMetricsCalculator` in `idp_common.evaluation.confidence_integration` computes calibration metrics at overall and per-field levels. Test aggregation results now include `confidence_metrics` field with comprehensive calibration data. Confidence aggregation logic moved from frontend to backend (test execution aggregation function) for cleaner architecture. Evaluation service patches `field_comparisons` with `field_path` for ConfidenceCalculator compatibility and uses structural detection to unwrap wrapper keys (Item_N, Record_N) from extraction results. Fully backward compatible. Test Studio now displays Error Capture at Review Budget (30%) showing percentage of errors caught when reviewing lowest-confidence 30% of data. Format: "46% (1.52x)". New column in field metrics table (gear-icon configurable) and in Additional Metrics section.

- **Metric info tooltips** — All Test Studio metrics now include info icons with explanatory tooltips. Covers accuracy metrics (Precision, Recall, F1, Accuracy), confidence calibration metrics (AUROC, ECE, Brier, ECARB@30, Coverage Ratio), confusion matrix components (TP, FP, TN, FN), error rates (False Alarm Rate, False Discovery Rate), aggregate metrics (Avg Confidence, Avg Accuracy, Avg Weighted Score), and split classification metrics (Page Level Accuracy, Split Accuracy With/Without Order, Total Pages/Splits). Tooltips link to Wikipedia or Stickler documentation. Available in both TestResults and TestComparison views. Clicking info icons does not trigger table sorting.
  
### Changed

- **Default Chat-with-Document model promoted to `us.anthropic.claude-opus-4-8:1m`** — chat defaults now point at the newer Opus generation. EU deployments map automatically to `eu.anthropic.claude-opus-4-8:1m` via `UpdateConfiguration`. Existing custom configs are unaffected.

### Fixed

- **Agentic extraction now respects 128K output limit for Claude Opus 4.7+** — `_build_model_config` previously matched these models against the generic `claude-(opus|sonnet|haiku)-4` regex and silently capped them at 64K, contradicting the 128K declared in `model_config_limits.yaml`. A dedicated branch now returns 128K for opus-4-7 and opus-4-8.

- **`idp-cli deploy --headless` no longer leaves dangling references in the headless template** — the headless transformer now strips `HasPublicArtifactsBucket` (orphaned after `VersionCheckResolverFunction` was removed) and removes `ChatWithDocumentProcessorFunction` (only invoked by removed AppSync resolvers, with dangling `UsersTable` / `GraphQLApi` refs). `CircuitBreakerManagerFunction` is now also converted to DynamoDB tracking mode so its AppSync status-publish hook is stripped. Additionally, `--headless` (template transform) no longer auto-sets the `EnableHeadless` CFN parameter — that parameter opts into the Private API Gateway and requires VPC infrastructure, which is an orthogonal concern. Users who want the Jobs API must pass `--parameters EnableHeadless=true,VpcId=...` explicitly.

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.5.13.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.5.13.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.5.13.yaml`

## [0.5.12]

### Added

- **Test Studio: Abort running test runs** — Test runs with status `QUEUED` or `RUNNING` can now be aborted from both the Web UI and CLI. The abort operation stops all pending document processing workflows, preserves results from already-completed documents, and updates the test run status to `ABORTED`. Metrics are automatically calculated for completed documents. The Web UI displays an "Abort" button next to running tests, and the CLI provides an `idp-cli abort-test-run` command with confirmation prompts. Aborted test runs show accurate completion counts (e.g., "48/50 files processed") and allow viewing partial results including evaluation metrics and cost breakdowns for successfully processed documents.

- **Optional `{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}` placeholder for classification prompts** ([#262](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/262)) — Pattern 2 classification `task_prompt` templates can opt in to a new placeholder that expands, per document type, to the class name, description, **and** schema attribute names. Renders as XML for `multimodalPageLevelClassification` and as a markdown table for `textbasedHolisticClassification`. Cost-neutral by default — only materialized when the template references it, with per-class attribute counts capped (default 50) to keep prompt cost predictable. Useful for schema-rich domains where similarly-named classes have very different extraction schemas. The Web UI Prompt Preview tab renders the substituted attributes for inspection. See [`docs/classification.md`](docs/classification.md#optional-class_and_attribute_names_and_descriptions-placeholder).

- **Cross-account Bedrock invocation via STS AssumeRole** ([#305](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/305)) — IDP processing Lambdas can now route all Bedrock traffic through a centralized "hub" AWS account. Set the new optional `BedrockHubRoleArn` parameter (with optional external-id / session-name) and the stack conditionally adds `sts:AssumeRole` to each Lambda's execution role. STS credentials auto-refresh via `DeferredRefreshableCredentials` so warm Lambdas survive past the 1-hour STS session. Covers the entire pipeline plus discovery, embeddings, Chat with Document, and Agent Companion (incl. Strands sub-agents). Fully additive — leaving the parameter empty preserves prior same-account behavior. **Out of scope:** BDA runtime, Bedrock Knowledge Bases, and `model_finetuning/`. See [`docs/cross-account-bedrock.md`](docs/cross-account-bedrock.md).

- **Distinguish MISSING pages from BLANK fields in extraction output** ([#317](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/317)) — for sparsely-populated multi-section forms where pages may be legitimately omitted, extraction can now distinguish fields whose source page was *present but empty* (BLANK) from those whose source page was *not submitted* (MISSING). Two new optional schema extensions, `x-aws-idp-page-types` and `x-aws-idp-source-page-types`, declare named page sub-types and which page types each property sources from. A regex-based resolver detects present page types, annotates the LLM prompt with `--- PAGE N [PageType] ---` markers, and (when enabled) post-processes the JSON to drop/null fields whose source pages are absent. Output gains optional `page_type_resolution` and `missing_fields_report`. Fully additive; the Document Schema editor adds form widgets for both extensions. See [`docs/missing-page-handling.md`](docs/missing-page-handling.md) and the [demo notebook](notebooks/usecase-specific-examples/multi-page-bank-statement/step3_extraction_with_missing_pages.ipynb).

- **Private (VPC-only) deployment — browser uploads route through the S3 Interface VPC Endpoint** — when `WebUIHosting=ALB`, the ALB nested stack provisions an S3 Interface VPC Endpoint and exposes its regional DNS name as a new `S3VPCEndpointDnsName` output. Web UI presigner Lambdas, `ApiHandlerFunction`, and config/dataset custom resources receive an `S3_ENDPOINT_URL` env var and use virtual-host addressing so SigV4 matches the VPCE DNS. Browser uploads and Lambda S3 traffic stay on the AWS backbone with zero public-internet egress.

- **Bring-Your-Own S3 VPC endpoint** — two new top-level parameters (`S3VpcEndpointIdOverride`, `S3VpcEndpointDnsNameOverride`) let customers with a central network account reuse an existing endpoint instead of having the IDP stack provision one. Both must be set together; CloudFormation `Rules` enforce the pairing.

- **`monitoring` (CloudWatch) Interface VPC Endpoint** — `scripts/vpc-endpoints.yaml` now provisions a CloudWatch Interface VPC Endpoint, gated by the new `CreateMonitoringEndpoint` parameter (default `true`). Required for the `DashboardMerger` custom resource to succeed in private mode. `scripts/check-vpc-endpoints.sh` updated to detect and skip pre-existing endpoints.

- **`LambdaSecurityGroupId` parameter on the ALB nested stack** — when supplied, the ALB S3 VPC Endpoint security group allows inbound 443 from the app Lambda SG so VPC-resident Lambdas can reach S3 through the same endpoint as ALB. Fixes a 5-minute hang in `ConfigurationCopyFunction` caused by SG mismatch.

### Changed

- **ALB nested stack S3 VPC endpoint policy scoped to same-account operations** — the endpoint policy allows a finite set of S3 actions on `arn:${AWS::Partition}:s3:::*` conditioned on `aws:PrincipalAccount` / `aws:ResourceAccount` matching the deployment account. Wildcard resource is necessary to avoid cyclic dependencies on parent-stack buckets; authorization is additionally enforced at the network (SG) and IAM (role + bucket policy) layers. See `docs/deployment-private-network.md`.

- **`scripts/generate_self_signed_cert.sh` uses a short fixed CommonName (`idp-self-signed`) with the full ALB hostname in `subjectAltName`** — internal ALB DNS names often exceed the X.509 64-char CN limit, causing openssl to abort. Modern browsers validate only against SAN, so this is RFC-correct and removes the silent failure.

### Fixed

- **Agentic extraction now supports `:1m` model IDs (1M context window)** ([#312](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/312)) — agentic extraction with `:1m` model ids (e.g. `us.anthropic.claude-opus-4-7:1m`) previously failed at ConverseStream with `ValidationException` because the agentic path forwarded the raw id to Strands' `BedrockModel`. `_build_model_config` now strips `:1m` and forwards the `anthropic_beta` header via Strands' `additional_request_fields`, matching the traditional Bedrock path. All `:1m` variants now work.

- **Bedrock Knowledge Base nested stack no longer left in `DELETE_FAILED` on update/delete** ([#315](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/315)) — two reliability fixes in `nested/bedrockkb/template.yaml`:
  - **Reliable `AWS::Bedrock::DataSource` deletion during sync** — the Delete handler now stops in-progress ingestion jobs and polls until terminal status (12-min deadline) before signalling SUCCESS, so CFN can delete the data source cleanly. Always reports SUCCESS on Delete (logs warnings) so a stuck job never blocks stack delete. IAM gains `Stop/Get/ListIngestionJobs`, timeout is 15 min, and ingestion functions `DependsOn` their schedulers to avoid races.
  - **Helper IAM roles now `DeletionPolicy: Retain`** — `DataSourceSchedulerRole` and `StartIngestionJobFunctionRole` are ephemeral helpers; marking them `Retain` decouples nested-stack delete from the deploying principal's `iam:DeleteRole` permission. Defensive fix for session policies that deny `iam:DeleteRole`. Retained roles are inert and can be deleted manually after the stack is gone.

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.5.12.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.5.12.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.5.12.yaml`
  

## [0.5.11]

### Added

- **"Update available" indicator in Web UI Deployment Info** — the Deployment Info section of the side nav now shows a small `Update` badge next to the deployed `Version: …` line whenever a newer published template is available on the public artifacts bucket. Hovering the badge opens a popover showing the deployed and latest versions; for **Admin** users, the popover includes a one-click "Update stack in CloudFormation →" link that deep-links to the AWS console with the new template URL pre-filled (review parameters before applying). **Zero-touch by default**: `idp-cli publish` auto-substitutes the new `PublicArtifactsBucket` / `PublicArtifactsPrefix` CloudFormation parameter defaults to point at the bucket and prefix it's publishing to, so customers deploying the published template get the indicator out of the box. Headless / private-network deployments can override `PublicArtifactsBucket=""` to disable the check. The check itself runs in a small Lambda resolver (`getLatestPublishedVersion`) that lists the public bucket via unsigned S3 reads and caches results for 10 minutes. The headless template transformer (`HeadlessTemplateTransformer`) strips the resolver, parameters, and Settings entries so headless / GovCloud builds remain UI-free with zero dangling references.

- **Chat-with-Document enhancements** — the Web UI "Chat with Document" feature has been substantially upgraded:
  - **Async streaming** — responses now stream token-by-token into the chat bubble so large documents and long-context models no longer hit AppSync's 30-second synchronous timeout.
  - **Markdown rendering in assistant replies** — headings, bullet/numbered lists, fenced code blocks, inline code, tables, block quotes, and links render as formatted HTML instead of raw markdown characters. Renders during streaming and at final.
  - **Dedicated `chat` configuration section** — independent from `summarization`, with its own `model`, `system_prompt`, `temperature`, `top_k`, `top_p`, and `max_tokens`. Backward compatible: configs without a `chat` section fall back to `summarization.*`.
  - **UI model selector on the Chat panel** — per-session model override, populated from the config's model enum; default comes from the document's own config version.
  - **Default chat model is `us.anthropic.claude-opus-4-7:1m`** — 1M-context by default so typical multi-hundred-page packets fit without hitting input-token limits. EU and GovCloud presets use their region-appropriate inference profiles.
  - **First-class support for Bedrock model-ID suffixes** — `:1m` (1M-context beta), `:priority` and `:flex` (service tiers) all work end-to-end when selected in the Chat panel dropdown.

### Changed

### Fixed

- **Config validation now checks max_tokens against model limits** — `idp-cli config-validate` now verifies that `max_tokens` is within the model's maximum output token limit, catching invalid configurations like `extraction.max_tokens: 16000` with `us.amazon.nova-lite-v1:0` (max 10,000 tokens) before deployment. New `_validate_max_tokens()` function checks all services (extraction, classification, assessment, summarization) against model-specific limits loaded from `config_library/model_config_limits.yaml`: Claude 4.x (64,000), Claude 3.x (8,192), Amazon Nova (10,000), default (4,096). Added `get_model_max_output_tokens()` utility to `bedrock/model_utils.py` for use by CLI validation only (Lambda functions continue to use hardcoded limits for runtime defense-in-depth).

- **Empty content array handling across all LLM services** — LLMs occasionally return empty content arrays (`content: []`) instead of the expected text response, causing `IndexError: list index out of range` when accessing `content[0]`. All affected services now check for empty arrays before accessing elements and raise descriptive errors with task context. Applied to classification (page-level and holistic), summarization, bedrock client helper, and model finetuning inference. Added 11 unit tests in `test_empty_content_array.py` covering all edge cases (empty, single-element, multi-element arrays).

- **LLM array wrapping in extraction and assessment services** — LLMs occasionally return single-element arrays `[{...}]` instead of objects `{...}` when generating JSON responses, causing Pydantic validation errors (`Input should be a valid dictionary [type=dict_type, input_value=[{...}], input_type=list]`). All affected services now automatically detect and unwrap single-element arrays with a warning log, while multi-element arrays are rejected with a clear error message. Applied to standard extraction, agentic extraction, assessment service, and granular assessment service.

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.5.11.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.5.11.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.5.11.yaml`

## [0.5.10]

### Added

- **Enhanced config validation** — `idp-cli config-validate` now validates Bedrock model IDs against `pricing.yaml` and checks that custom `task_prompts` include required placeholders (e.g. `{DOCUMENT_TEXT}`, `{DOCUMENT_IMAGE}`) across all pipeline sections. Runs automatically on `config-upload` (use `--no-validate` to skip).

- **`idp-cli discover --model-id` flag** — override the Bedrock model used by `idp-cli discover` for a single invocation (e.g. `--model-id us.anthropic.claude-opus-4-6-v1`). Applies to all discovery modes; backward-compatible. ([#309](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/309))

- **Bedrock circuit breaker** — a CFN-parameterized circuit breaker that pauses new workflow starts when Bedrock is unhealthy and auto-recovers once the service comes back, so transient Bedrock outages no longer burn through SQS retries or leave documents half-processed. Off by default for full backward compatibility.
  - New `circuit_breaker_manager` Lambda owns state transitions (`CLOSED` / `OPEN` / `HALF_OPEN`) in the existing `ConcurrencyTable`. Triggered by CloudWatch Alarms on Bedrock error metrics (via SNS) and by an EventBridge-scheduled health check that promotes `OPEN → HALF_OPEN` after `RECOVERY_TIMEOUT_SECONDS`. `workflow_tracker` closes the breaker after the first successful probe.
  - `queue_processor` gates new work before incrementing the concurrency counter; `OPEN` messages are redelivered by SQS (with `ChangeMessageVisibility` extended to the recovery timeout to avoid DLQ churn), DDB errors fail open.
  - All state transitions use conditional DDB writes so concurrent alarm/workflow updates cannot clobber each other. `failure_count` is preserved across `OPEN → HALF_OPEN`; manual reset zeros counters and clears `last_error`.
  - Operator hooks: manual `reset` / `get_state` invocations, optional customer Lambda invoked via `ERROR_HANDLER_ARN`, CloudWatch metrics (`CircuitBreaker{Opened,HalfOpen,Closed}`), and `AlertsTopic` notifications.
  - New CFN parameters (all default off): `EnableCircuitBreaker`, `CircuitBreakerRecoveryTimeoutSeconds`, `CircuitBreakerErrorHandlerArn`. Unit tests cover alarm/health-check/manual/race-loss branches.
  - **Web UI visibility & admin controls** — document list header shows a live status badge (green/blue/red with `lastError` tooltip) via an AppSync subscription; clicking opens a details panel. Admin-group users additionally get **Pause / Resume / Probe** buttons (each requires a reason, persisted and broadcast). All automatic transitions publish to the subscription so the badge updates within ~1s. Hidden entirely when `CircuitBreakerEnabled=false`. Backed by new AppSync ops (`getCircuitBreakerStatus`, `pause/resume/probeCircuitBreaker`, `onCircuitBreakerStatusChange`) and a new resolver Lambda that enforces Admin authorization at both the schema and resolver layers.
  - Docs: `docs/circuit-breaker.md` and `src/lambda/circuit_breaker_manager/README.md`.

### Changed

- **Replaced DSR with open-source SRT security scanning tool** — Migrated from the deprecated internal DSR tool to the open-source [Sample Security Review Tool (SRT)](https://github.com/aws-samples/sample-security-review-tool). GitLab CI/CD now runs SRT on MRs targeting `develop` and fails the pipeline on findings. New Makefile targets: `make srt`, `make srt-setup`, `make srt-scan`, `make srt-fix`.

### Fixed

- **SRT now uses `--no-license-update`** to prevent it from automatically rewriting source file license headers during security scans.

- **Agentic extraction with Claude Opus 4.7 no longer fails with `top_p is deprecated`** — the Claude 4.7+ enablement in v0.5.7 fixed the traditional Bedrock path but missed the Strands-based agentic path (`idp_common/extraction/agentic_idp.py`), which still forwarded `top_p` to ConverseStream. Both paths now share the same `is_claude_4_7_model` detection and omit deprecated inference params. ([#304](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/304))

- **`idp-cli discover` silently ignored mismatched ground truth files** — previously a filename-stem mismatch between `-d` and `-g` only produced a warning and ran discovery without ground truth. Now: single-doc + single-GT invocations are paired by position (no filename match required); batch-mode mismatches or duplicate GT stems fail with exit `1` and a clear message. ([#310](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/310))

- **Web UI "View Source" failed for PDFs and other docs after the v0.5.9 CSP hardening** — three fixes in `FileViewer`: (1) pass an `s3://bucket/key` URI to `getFileContents` instead of relying on the build-time `VITE_AWS_REGION` env var; (2) render PDFs in an `<iframe>` instead of `<object>` so they're allowed under the hardened `object-src 'none'` CSP; (3) drop the `sandbox` attribute on the PDF iframe only (Chrome's built-in PDF viewer is blocked when sandboxed; non-PDF iframes keep their sandbox). Added a fallback "Open PDF in a new tab" link.

- **Private ALB deployment broken when stack name had uppercase characters** — the ALB DNS name is case-preserving, but browser `Origin` headers, Cognito `redirect_uri` matching, and the ALB url-rewrite regex all expected lowercase, so CORS preflights, OAuth callbacks, and the `/` → `/index.html` rewrite all failed. Fixed by lowercasing the ALB URL in every CFN consumer (new `GetLowercaseAlbUrl` custom resource reusing the existing `GetDomainLambda`), lowercasing the Amplify redirect URL in `aws-exports.js`, and broadening the ALB rewrite regex from `^/$` to `^/` so OAuth query strings don't break the match. CloudFront deployments unaffected. ([#303](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/303))

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.5.10.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.5.10.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.5.10.yaml`

## [0.5.9]

### Added

- **Policy Discovery & Rule Validation Policy Classification**: Upload a regulatory document (e.g., an NCCI Medicare policy manual) and automatically extract structured validation rules from it. A new "Policy Discovery" tab in the Discovery page walks you through the process, and the extracted rules feed directly into the rule validation workflow.
  - A new policy classification step runs before rule validation, matching each document against your configured `policy_classes` using regex patterns on document names and page content. Only matching policy rules are evaluated, so unrelated rules are skipped automatically.
  - The configuration key `rule_classes` has been renamed to `policy_classes` for clarity. Existing configs will need to update this key.
  - The Schema Builder now has dedicated support for editing policy classes with policy-specific labels, and extraction-only settings are hidden when editing policy schemas.
  - A "Policy Discovery" section has been added to Discovery Configuration in the UI, letting you choose the model, temperature, and prompts used for Policy Discovery.
  - The legacy `rule-extraction` configuration preset has been removed. Use **Policy Discovery** on the Discovery tab instead — it writes extracted rules directly into the active config's `policy_classes`.

- **Document-level Download button on the Document Details page** — A new **Download** dropdown in the Document Details header lets users pull every output artifact for a document in a single click, packaged as a ZIP. Three scopes are offered:
  - **Download All (ZIP)** — document attributes, metering, summary, evaluation & rule-validation reports, per-section predictions, baselines (when available), per-page text/confidence, and optionally per-page images and/or the source document (checkboxes).
  - **Download Predictions (ZIP)** — all section result JSONs plus a self-describing `manifest.json`.
  - **Download Baselines (ZIP)** — all baseline section result JSONs (shown only when an evaluation baseline is available).
  - **Bucket-mirrored ZIP layout** — files are organised under top-level `output/`, `baseline/`, and `input/` folders that preserve the real S3 key structure, so the archive can be diffed with a direct `aws s3 sync` of the same buckets.

- **Headless REST API mode with VPC-secured deployment for GovCloud** — a first-party Jobs REST API for programmatic document submission and status tracking, plus an optional VPC-secured deployment that keeps the API off the public internet. Makes end-to-end GovCloud deployment viable without the UI/AppSync stack, and gives Commercial customers a supported alternative to direct S3 uploads for machine-to-machine integrations.
  - **Jobs REST API** (new `src/lambda/api_handler/`, `src/lambda/job_tracker/`, `src/lambda/batch_pre_processor/`):
    - `POST /jobs` — creates a job record and returns a presigned POST URL for the input zip (1-hour expiry, content-type pinned to `application/zip`, 5 GB content-length cap).
    - `GET /jobs/{job_id}` — returns overall status (`PENDING_UPLOAD` / `IN_PROGRESS` / `SUCCEEDED` / `PARTIALLY_SUCCEEDED` / `FAILED` / `ABORTED`), per-file status map, and — on success — a presigned GET URL for `results.zip`. `SUCCEEDED` is gated on `results.zip` actually being present in the output bucket to avoid racing callers into a 404.
    - OAuth2 `client_credentials` auth via a dedicated Cognito User Pool + Resource Server (`idp-api/jobs.read`, `idp-api/jobs.write` scopes). Separate from the existing web-UI Cognito pool.
    - **Per-client job ownership (M1):** each job records its creating Cognito principal (`sub` / `client_id`) as `CreatedBy`. `GET /jobs/{job_id}` returns **HTTP 404** (not 403, to avoid existence-leak) when the caller's principal doesn't match the job's owner. Legacy job records written before this field existed remain readable by any authenticated caller. **Behavior change:** `GET /jobs/{job_id}` on a non-existent job now correctly returns 404; previously returned 400 (a pre-existing response-code bug in the API handler).
  - **Private API Gateway + bastion tunneling:**
    - `AWS::Serverless::Api` with `EndpointConfiguration: PRIVATE` bound to a customer-supplied `ApiGatewayVpcEndpointId` and a resource policy that denies all traffic not originating from that VPC endpoint.
    - Optional `DeployBastionHost=true` spins up an SSM-reachable `t3.small` EC2 with IMDSv2 required, encrypted EBS via a dedicated rotating KMS key, and no inbound SSH. `scripts/bastion.sh <STACK_NAME>` sets up a local SSH tunnel for dev-time API access; `scripts/get_api_token.sh <STACK_NAME>` fetches an OAuth2 bearer token.
  - **Safe zip extraction in `batch_pre_processor` (M2 + M3):**
    - `MAX_UNCOMPRESSED_BYTES` (default 20 GiB, env-configurable) and `MAX_ENTRIES` (default 10,000) bounds checked **pre-flight** before any uploads begin. Bound violations write a terminal `FAILED` marker to the job record so the API surfaces the failure.
    - Per-entry streaming via `zipfile.ZipFile.open()` + `s3.upload_fileobj()` — no more loading whole entries into Lambda memory.
    - Per-entry failure isolation — one bad file is marked `FAILED` and the rest of the batch still uploads and advances through the pipeline; the job converges to `PARTIALLY_SUCCEEDED` / `FAILED` / `SUCCEEDED` as appropriate.
  - **New CFN parameters** (all default to off/empty, fully backward-compatible):
    - `EnableHeadless` (bool) — turns on the Jobs REST API.
    - `DeployInVPC` (bool) — places all IDP Lambdas in customer-supplied private subnets with a customer-supplied security group.
    - `VpcId`, `PrivateSubnetIds`, `ApiGatewayVpcEndpointId`, `LambdaSecurityGroupId`, `ApiStageName` — customer-supplied networking.
    - `DeployBastionHost`, `BastionHostSubnetId`, `BastionHostSecurityGroupId` — optional dev-access bastion.
    - **CloudFormation console UX** - the 11 new parameters are grouped into two dedicated `AWS::CloudFormation::Interface` sections ("Headless API Deployment (required for GovCloud)" and "Headless API Deployment - Bastion Host (optional, requires VPC Secured Mode)") with friendlier `ParameterLabels` and rewritten `Description` text. Each description now explicitly states when the parameter is required, what the default behavior is (no Jobs API / no Lambda VPC placement / no bastion EC2 unless explicitly enabled), and which companion parameters it depends on. Ensures Quick-Start users who click the README's "Launch Stack" button see clear opt-in sections rather than assuming the bastion host or Jobs API is always deployed.
  - **CFN fail-fast validation (H1)** — new `Rules:` block entries catch misconfiguration at stack create / update time with clear `AssertDescription` errors, instead of failing deep in resource provisioning:
    - `HeadlessRequiresVPC` — `EnableHeadless=true` requires `DeployInVPC=true` + non-empty `VpcId` / `ApiGatewayVpcEndpointId` / `LambdaSecurityGroupId`.
    - `BastionRequiresVPC` — `DeployBastionHost=true` requires `DeployInVPC=true` + non-empty bastion subnet / SG.
  - Plus **defense-in-depth** on the two API-gated Lambdas: `VpcConfig` is wrapped in `!If [DeployInVPC, …, AWS::NoValue]` so even if the Rules block is ever relaxed, the Lambdas won't fail to create on empty `!Ref` values.
  - **CLI (`idp-cli`):**
    - `--headless` now auto-sets the `EnableHeadless=true` stack parameter — they were always used together.
    - `idp-cli deploy --headless --from-code . --stack-name <NEW>` no longer requires `--admin-email`. The headless template strips the UI Cognito pool and has no `AdminEmail` parameter; passing it through produced `ValidationError: Parameters: [AdminEmail] do not exist in the template`. Now skipped and dropped with a note. Non-headless new-stack creation still requires `--admin-email`.
  - **Publish pipeline fixes that make headless-to-GovCloud deploys work:**
    - `cfn-lint` in headless mode now lints `idp-headless.yaml` and skips commercial-only templates (`idp-main.yaml`, `nested/appsync`), which contain `AWS::AppSync::*` / `AWS::CloudFront::*` resources that don't exist in `us-gov-*` regions. Fixes `E3006 Resource type … does not exist`.
    - E/W classification in `_validate_cfn_lint` now uses `^E\d{4}` / `^W\d{4}` regex anchors. Previously the substring `":E"` also matched resource prefixes like `AWS::EC2::`, inflating warning-severity lines to errors.
    - `WorkflowStateChangeRule` JobTracker target moved from a conditional `Arn` field (flagged `E3003 'Arn' is a required property`) to a conditional full-target dict via `!If`.
  - **Documentation:**
    - New `docs/govcloud-batch-api.md` — REST API reference with schemas, OAuth flow, bastion tunneling setup, and an Authorization model section covering per-client ownership and multi-client behavior.
    - New `docs/govcloud-architecture.md`, `docs/govcloud-operations.md`, `docs/vpc-secured-mode.md`.
    - Overhauled `docs/govcloud-deployment.md` with a deployment-variant matrix (Vanilla / Headless API / Headless + VPC / Headless + VPC + Bastion).
  - **End-to-end test script:** `scripts/e2e_test_headless.py <STACK_NAME> <PATH_TO_FILE>` exercises the full flow (OAuth → POST /jobs → presigned upload → status poll → download results).

- **Managed configuration upload rejection** — `idp-cli config upload` now rejects configuration files with `managed: true` to prevent users from accidentally creating stack-managed configurations that would be overwritten on stack updates. All user-uploaded configurations automatically have `managed: false` set, ensuring they persist across stack lifecycle events.

### Fixed

- **Evaluation markdown/report rendering resilience** — two defensive fixes that keep evaluation and test-results pages from crashing when upstream data is non-numeric or empty.

### Security

Hardening response to security review - Highlights:

- **Stored XSS defense-in-depth (frontend).** Introduced
  `SafeMarkdown` wrapper (`src/ui/src/components/common/SafeMarkdown.tsx`)
  that pairs `rehype-raw` with `rehype-sanitize` using an allow-list
  schema (retains `<details>`/`<summary>`, custom `<documentid>`,
  tables, code blocks, and a narrow `white-space: pre-line` style
  pattern; strips `<script>`, event handlers, `javascript:` URLs,
  `<iframe>`, `<object>`, `<embed>`). Migrated all six legacy
  `ReactMarkdown + rehypeRaw` call sites across
  `MarkdownViewer.tsx`, `DocumentsQueryLayout.tsx`,
  `TextDisplay.tsx`, `AgentChatLayout.tsx`, and `AgentToolComponent.tsx`.
- **Stored XSS fix in Knowledge Base resolver (backend).**
  `query_knowledgebase_resolver` now HTML-escapes citation snippets,
  document titles, and URLs via `html.escape()` before embedding them
  in the rendered markdown.
- **Chat session ownership enforced.** `getChatMessages`
  (`get_agent_chat_messages_resolver`) now verifies that the calling
  Cognito user owns the requested `sessionId` by looking up
  `(userId, sessionId)` in `ChatSessionsTable`. Can be temporarily
  disabled via `ENFORCE_CHAT_SESSION_OWNERSHIP=false` env var for
  legacy-session migration. Fails closed on DynamoDB errors.
- **S3 URI allow-list in `getFileContents`.** The resolver now
  rejects any `s3Uri` whose bucket is not one of the IDP stack's
  configured buckets, preventing use as a generic S3-read gadget.
  Also fixes a latent bucket-name parsing bug and validates the
  URI scheme.
- **Log sanitization utility.** New
  `idp_common.utils.log_sanitizer.sanitize_event_for_logging()`
  deep-copies and redacts Cognito claims, identity blobs, auth
  tokens, and API keys from events before they are emitted to
  CloudWatch. Truncates common document-content fields to 500
  characters. Applied to `reprocess_document_resolver`,
  `query_knowledgebase_resolver`, and `get_agent_chat_messages_resolver`
  as reference integrations (rollout to remaining resolvers is tracked
  for a follow-up release). 15 unit tests added.
- **CSP hardening (Phase 1).** Tightened CloudFront
  `SecurityHeadersPolicy`: `object-src 'none'` (was
  `'self' blob: data: https:`), `connect-src` restricted to AWS
  service hostnames (was `https:`). `unsafe-eval` / `unsafe-inline`
  removal deferred pending Monaco-editor compatibility verification.
- **False-positive documentation.** Added explanatory comments and
  `nosec` justifications for:
  - Jinja2 autoescape disabled in `discovery_agent.py`
    (templates produce LLM prompts, not HTML).
  - Unsafe `yaml.load` findings in
    `scripts/sdlc/validate_service_role_permissions.py` and
    `lib/idp_sdk/idp_sdk/_core/publish.py` (both use
    `CFNLoader`/`CFLoader` subclassing `yaml.SafeLoader`; input is
    developer-committed CloudFormation templates, not user input).
  - SQL injection in `test_results_resolver` Athena queries (every
    interpolation is gated by `_validate_sql_input()` with a strict
    allow-list regex).

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.5.9.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.5.9.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.5.9.yaml`
  

## [0.5.8]

### Added

- **Excluded-class feature — skip static instruction / legal / boilerplate pages** — Government forms and similar packages often bundle static informational pages (legal warnings, fee instructions, tax notices, oaths) alongside the pages that carry applicant data. Mark a document class with `x-aws-idp-exclude-from-processing: true` and all downstream stages (extraction, assessment, summarization, rule validation, evaluation) skip sections classified as that class — making **zero LLM calls** on boilerplate pages.
  - Optional `x-aws-idp-exclusion-reason` ("instructions", "legal", "cover-page", …) surfaces as a grey **`Skipped: <reason>`** badge in the UI Sections panel and as an **"Excluded Sections (Not Evaluated)"** table in the evaluation markdown report.
  - Configurable via the **UI Configuration Editor** → Document Schema → select a document-type class → "Exclude from Processing" checkbox + "Exclusion Reason" input.
  - New end-to-end sample config at `config_library/unified/ds11-passport-application/` with a matching DS-11 U.S. Passport Application PDF fixture and a standalone demo notebook (`notebooks/usecase-specific-examples/ds11-passport-application/`).
  - Additive: classes without the new flag behave exactly as before.
  - See `docs/classification.md#excluding-static-pages-eg-instructions-legal-boilerplate`.

### Changed

- **UI dependency cleanup — eliminated 11 of 12 npm deprecation warnings** — Replaced deprecated `@aws-sdk/*` packages with `@smithy/*` equivalents, removed unused Babel plugins, migrated ESLint 8→9 (flat config), upgraded Prettier 2→3, and upgraded jsdom 26→29. Added `"type": "module"` to `package.json`. Also added `caughtErrors: 'none'` to ESLint config to stop flagging unused catch clause variables. Added `FORCE=1` arg to `make ui-lint` to force re-run despite checksum match.

- **Headless deployment documentation generalized** — headless mode is no longer documented as a GovCloud-only capability. New `docs/headless-deployment.md` is the canonical guide covering headless deployment for both Commercial and GovCloud regions (API-only / pipeline integrations, organizational restrictions on UI-layer services, cost optimization, and required for GovCloud). 

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.5.8.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.5.8.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.5.8.yaml`
  
  
## [0.5.7]

### Added

- **Claude Opus 4.7 Model Support** — Added `anthropic.claude-opus-4-7` (and `:1m` context variant) across all `us`, `eu`, and `global` inference profiles. Includes unified template enums, UI model dropdowns, cachepoint support, EU region mappings, pricing entries, and documentation updates.

- **Add Documents to Existing Test Sets** — New "Add Documents" action in Test Studio allows incrementally adding documents (with ground truth) to an existing test set. Supports both "From Existing Files" (S3 pattern) and "From Upload" (ZIP) sources. Key features:
  - **Automatic baseline filtering**: When using the Input Bucket, files without matching baseline/ground truth data are automatically excluded rather than failing the operation, with a result message reporting counts (e.g., "Added 8 of 12 files (4 excluded - no baseline data)")
  - **Time filter**: Optional "Modified after" filter with presets (Last 1 hour, 4 hours, 24 hours, 7 days, 30 days) and a custom date/time picker, available in both new test set creation and add-documents flows
  - **Idempotent**: Re-adding an existing document overwrites it; file counts are always recounted from S3 for accuracy
  - **UPDATING status**: Test sets show a transient "Updating..." badge while documents are being added

- **Creating Custom Test Sets Guide** — New tutorial-style documentation (`docs/creating-custom-test-sets.md`) walking through the end-to-end workflow for creating custom test sets with ground truth data from scratch: configure for max accuracy, discover document schema, process samples, review/edit predictions, save evaluation baselines, register test sets, and run comparative test executions to evaluate cost vs. accuracy tradeoffs. Referenced from `docs/demo-videos.md`.
  
- **Configuration Version Tracking Across All Analytics Tables** — Added `config_version` field to all analytics tables (metering, document_evaluations, section_evaluations, attribute_evaluations, and document_sections_*) to enable comprehensive tracking and analytics per configuration version. All Glue tables now include a `config_version` column, and all Parquet files store the configuration version used for each document. Enables direct filtering and comparison queries without complex JOINs - users can query "show me W2 documents processed with config v2.1" or "compare accuracy for configs v2.0 vs v2.1" with simple WHERE clauses. Supports cost analysis, A/B testing, quality comparison, and data lineage tracking. Documents without a config version default to "default".

### Fixed

- **Incorrect global inference profile IDs for Knowledge Base model** — Fixed `global.anthropic.claude-haiku-4-5-v1:0` and `global.anthropic.claude-sonnet-4-5-v1:0` in the `KnowledgeBaseModelId` CloudFormation parameter dropdown. These shortened IDs were invalid and caused `ResourceNotFoundException` when used. Corrected to `global.anthropic.claude-haiku-4-5-20251001-v1:0` and `global.anthropic.claude-sonnet-4-5-20250929-v1:0` per the [AWS Bedrock inference profiles documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html). ([#286](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/286))

- **Application Inference Profile IAM permissions** — Added `application-inference-profile/*` ARN pattern to `bedrock:InvokeModel` IAM policies across all templates (root, appsync, multi-doc-discovery, and sample templates). PR #236 previously fixed only `patterns/unified/template.yaml`; this completes the fix for all Lambda execution roles. Also added `bedrock:GetInferenceProfile` read permission to support prompt caching resolution. ([#272](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/272))

- **Prompt caching with application inference profiles** — Fixed `<<CACHEPOINT>>` tags being stripped when using Bedrock application inference profile ARNs as model IDs. The cachepoint check now resolves inference profile ARNs to their underlying foundation model via the `GetInferenceProfile` API, enabling prompt caching for profiles that wrap supported models (Claude, Nova). Results are cached to avoid repeated API calls, with graceful fallback if the API call fails. ([#272](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/272))

- **Chat with document uses hardcoded US model ID** — Fixed "Chat with document" feature failing in non-US regions (e.g., `eu-west-1`) with "The provided model identifier is invalid" error. The backend Lambda's `get_summarization_model()` fallback was hardcoded to `us.amazon.nova-pro-v1:0`. Added `get_default_model_for_region()` helper that selects the appropriate region-prefixed model (`eu.amazon.nova-pro-v1:0` for EU, `us.amazon.nova-pro-v1:0` for US) based on `AWS_REGION`. ([#282](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/282))

- **BDA activation modal checking wrong version config** — Fixed the "Activate Version" flow incorrectly checking the *currently selected* version's `use_bda` flag (`mergedConfig?.use_bda`) instead of the *target* version being activated. This caused the BDA sync confirmation modal to appear (or not appear) based on the wrong version's configuration. The fix fetches and inspects the target version's actual config before deciding whether to show the modal. Also added a `fetchVersions()` refresh after BDA sync operations to keep BDA project ARN metadata up to date in the versions list.

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.5.7.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.5.7.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.5.7.yaml`
  
  
## [0.5.6]

### Added

- **Test Studio CLI Commands** — `idp-cli test-result` to retrieve test results with automatic evaluation triggering and `--wait`/`--output-dir` options, and `idp-cli test-compare` to compare multiple test runs with JSON/CSV export. See `docs/idp-cli.md`.

- **Custom Model Fine-Tuning** — Fine-tune Amazon Nova 2 models (Lite and Pro) for document classification and extraction using your own labeled Test Sets. The end-to-end workflow — validate data, generate training data, train via Bedrock, and deploy an on-demand custom model endpoint — is driven from a new **Custom Models** page in the Web UI. Custom models can then be selected in any configuration version for classification and/or extraction. Available to Admin and Author roles. **Note:** currently requires deployment in `us-east-1`. See `docs/custom-model-finetuning.md`.
  
- **External SAML/OIDC Identity Provider Federation** — Optional support for federating authentication through an external SAML or OIDC identity provider via Amazon Cognito. Enables organizations to use existing enterprise identity providers (PingOne, Okta, Microsoft Entra ID, etc.) for single sign-on. All federation functionality is opt-in through 12 new CloudFormation parameters — leaving them empty results in zero additional resources and identical behavior to existing Cognito-native authentication. See `docs/external-idp.md`.

- **Private Network Deployment** — Deploy the IDP Accelerator in fully private / air-gapped environments. New `AppSyncVisibility` parameter (`GLOBAL` | `PRIVATE`) makes the AppSync API accessible only from inside the VPC. All processing Lambda functions (21 across 3 templates) are conditionally placed in customer VPC subnets with an HTTPS-only security group. Includes a separate VPC endpoint CloudFormation template (`scripts/vpc-endpoints.yaml`) with 16 interface endpoints (AppSync, Bedrock, SQS, DynamoDB, S3, Lambda, SSM, KMS, STS, Textract, and more) and per-endpoint creation flags to skip pre-existing endpoints. All features are off by default — existing deployments are completely unaffected. See `docs/deployment-private-network.md`.

- **Enhanced Information Panels** — Added comprehensive help content to the Information (ⓘ) panel on every page in the Web UI. Each panel now includes a feature summary, list of key capabilities, and "Learn more" links to relevant docs-site documentation pages. Created new panels for 8 pages that previously had none (Pricing, Capacity Planning, Custom Models, Discovery, User Management, Test Studio), and enriched the existing 7 panels with fuller descriptions and documentation links.
  
### Changed

- **Removed Claude Sonnet 4:1m and Sonnet 4.5:1m model variants** — The 1M context window beta for Claude Sonnet 4 (`claude-sonnet-4-20250514-v1:0:1m`) and Sonnet 4.5 (`claude-sonnet-4-5-20250929-v1:0:1m`) is being retired effective April 30, 2026. These `:1m` model variants have been removed from all enum lists, UI dropdowns, quota code mappings, pricing, and documentation. Users needing 1M context windows should migrate to Claude Sonnet 4.6 (`claude-sonnet-4-6:1m`), where the 1M context window is generally available (GA).

- **Default extraction model updated** to `us.anthropic.claude-sonnet-4-6` (was `us.anthropic.claude-sonnet-4-20250514-v1:0`) in system defaults.
- **Error Analyzer system prompt improvements** — Added strategy for large batches, priority ordering, and error classification guidance.
- **Error Analyzer settings** — Replaced duplicate inline cache with the shared cache from the common monitoring package.
- **Shared CloudWatch Logs** — Extracted log search logic from the Error Analyzer into a reusable library in the common monitoring package.
- **Enhanced CI/CD Automated Testing** — Enhanced GitLab CI/CD pipeline smoke tests with parallel test execution (8 tests running concurrently with fail-fast behavior), deeper verification (extraction fields, classification results, rule statistics), and added new tests: multi-document concurrent processing (Test 4), Test Studio evaluation with metrics validation (Test 7), agentic extraction with large table validation - 532 fund items (Test 8), single-document discovery (Test 9), and multi-document discovery (Test 10).

### Fixed

- **Fixed** agentic extraction crash (`TypeError: unsupported format string passed to NoneType.__format__`) when table parsing stats contain `None` values for `avg_confidence` or `parse_success_rate`.
- **Fixed** agentic extraction `map_table_to_schema` producing phantom empty rows from non-matching tables (e.g. account_summary rows prepended to transaction_details), causing list item ordering to be shifted by several positions.
- **Error Analyzer model selection** — The agent was using the Chat Companion's model instead of its own configured model.
- **Error Analyzer log processing** — Fixed early termination that stopped searching after the first Lambda function with errors; now searches all relevant log groups.
- **Error Analyzer log truncation** — Fixed handling of long log messages to trim them rather than skip them entirely.
- **Reprocess from Document Details** — Fixed config version not being passed when reprocessing a document from the Document Details page (showed "N/A" instead of the selected version).
- **Analytics Agent date awareness** — Injected current UTC date/time into the analytics agent system prompt so the LLM can correctly handle relative-time queries (e.g., "show me today's documents", "what was processed this week").

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.5.6.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.5.6.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.5.6.yaml`

## [0.5.5]

### Added

- **Multi-Document Discovery** — New capability to automatically discover document classes from a collection of documents. Instead of manually defining document schemas one at a time, users point to a folder of mixed documents and the system automatically identifies document types, clusters similar documents, generates JSON Schemas with field definitions for each type, and saves them to a configuration version — ready for immediate use in the processing pipeline. Available from the Web UI, CLI (`idp-cli discover-multidoc`), and SDK (`client.discovery.run_multi_doc()`).
  - **Web UI**: New "Multi-Document" tab on the Discovery page with job submission form (config version selector, bucket selector, S3 prefix input, zip upload), jobs table with search/filter/sort/pagination, and detailed job results page with pipeline progress, expandable JSON schemas, config deep-links, and Quality Review Report
  - **CLI**: `idp-cli discover-multidoc --dir ./samples/ -o ./schemas/` with Rich progress bars, results table, and reflection report
  - **SDK**: `client.discovery.run_multi_doc(document_dir="./samples/")` with typed `MultiDocDiscoveryResult` response model
  - **Two Input Modes**: S3 path (select bucket + prefix), zip upload (presigned URL), or local directory (CLI/SDK)
  - **Configuration Integration**: Discovered classes are saved directly to the selected config version's `classes` array in DynamoDB, immediately available for document processing without manual schema creation

- **Prompt Preview** — New "Prompt Preview" tab in the Configuration page lets you preview the actual prompts sent to the LLM for each processing step (Classification, Extraction, Assessment, Summarization). Config-derived placeholders are filled in with real values (class names, cleaned JSON Schema), while document-specific placeholders are shown as highlighted markers. Includes token estimates, copy-to-clipboard, and a substitution details panel showing the exact schema sent to the LLM. Helps optimize document class schemas and prompt templates.

- **IDP CLI `chat` Command & SDK `ChatOperation`** — Interactive Agent Companion Chat from the terminal and programmatic SDK access. Runs the same multi-agent orchestrator as the Web UI locally, with real-time streaming and multi-turn conversation support. Includes Analytics Agent, Error Analyzer Agent, and optionally Code Intelligence Agent (`--enable-code-intelligence`). Available as `idp-cli chat --stack-name <stack>` for interactive use, `--prompt` flag for single-shot scripting, and `client.chat.send_message()` in the Python SDK. See `docs/idp-cli.md#chat`.

- **Per-Class Extraction Model Override** — New JSON Schema extension allows overriding the global `extraction.model` on a per-document-class basis. Useful when certain document types benefit from a different model (e.g., a more powerful model for complex financial forms, a faster/cheaper model for simple documents). Classes without the extension continue to use the global default. Works with both traditional and agentic extraction modes. See `docs/extraction.md` — Per-Class Extraction Model Override section.

- **Chandra OCR Lambda Hook Sample** — New `GENAIIDP-chandra-ocr-hook` sample in `samples/lambda-hook-inference/` that integrates [Datalab Chandra OCR 2](https://github.com/datalab-to/chandra) with the LambdaHook feature for high-quality OCR. Supports 90+ languages, math, tables, forms, and handwriting. Uses the Datalab hosted async API (`/api/v1/convert`) with configurable output format (markdown/json/html) and conversion mode (fast/balanced/accurate). Includes standalone SAM template, local test script, and deployment instructions. See `docs/lambda-hook-inference.md` — Chandra OCR Integration section.

- **Average Cost Per Page Metric** — Test results and test comparison views now display an "Avg Cost/Page" metric, calculated from total cost and page counts in the cost breakdown. Also included in CSV and JSON exports from the comparison view.

- **Wildcard pattern support for delete-documents** — `idp-cli delete-documents` and `client.batch.delete_documents()` now accept a `--pattern` / `pattern` parameter for fnmatch-style wildcard matching (e.g. `"batch-123/*.pdf"`, `"*invoice*"`). Combines with `--status-filter` to delete e.g. all failed invoices across batches.

- **Agentic Extraction Hardening** — Improved robustness, observability, and table parsing for agentic extraction:
  - Pre-flight OCR & schema analysis with adaptive guidance strength (RECOMMENDED → STRONGLY_RECOMMENDED → MANDATORY) ensures table parsing tool is used for large tables
  - Deterministic Markdown table parser with lookahead recovery, auto-merge of split tables, and configurable `max_empty_line_gap`
  - Post-extraction completeness validation against schema constraints with detailed shortfall reporting
  - Processing report with tool usage decisions, completeness checks, and root cause diagnostics (new UI tab + CloudWatch logs)
  - Thread-safe state management via `contextvars.ContextVar`; deprecated review agent (config fields preserved as no-ops)
  - Bug fixes: `patch_buffer_data` slice correction, confidence assessment loop fix, row-based parse success metric, NoneType guard in completeness check

### Fixed

- **Headless deployment fails with `ConfigurationPreset` AllowedValues error and `GraphQLApi.Arn` reference error** — Added `lending-package-sample-govcloud` to the base template AllowedValues and ConfigurationMap, and auto-detect GovCloud region (`us-gov-*`) for headless template transform instead of missing or hardcoded flag. Also added Discovery resources (BlueprintOptimization, MultiDocDiscovery, DiscoveryProcessor, etc.) to headless removal list to fix `GraphQLApi.Arn` unresolved reference error.

- **`delete-documents` fails with DynamoDB errors** — Fixed two bugs in `get_documents_by_batch()`: (1) passing empty `ExpressionAttributeNames={}` when no status filter caused `ValidationException`, and (2) using low-level DynamoDB client type descriptors (`{"S": "..."}`) with the high-level Table resource caused `begins_with` operand type mismatch. Rewrote to use the high-level `Table.scan()` API with `boto3.dynamodb.conditions.Attr`.

## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.5.5.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.5.5.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.5.5.yaml`

## [0.5.4]

### Added

- **MLflow Experiment Tracking Integration** — Optional integration with Amazon SageMaker MLflow for automated test run logging. When enabled (`EnableMLflow=true`), every Test Studio run automatically logs metrics (accuracy, cost, field-level scores), configuration parameters (model IDs, temperatures, inference settings), and artifacts (full config snapshots, class definitions, cost breakdowns) to an MLflow tracking server. Fire-and-forget async invocation — never blocks or delays test results. Zero resources created when disabled. See `docs/mlflow-integration.md`.

- **BDA Blueprint Optimization** — Automatically improves BDA extraction accuracy using the `InvokeBlueprintOptimizationAsync` API. When discovery includes a ground truth file and `enable_blueprint_optimization: true` is set, the system optimizes the BDA blueprint by comparing extraction results against ground truth, evaluates before/after metrics, and updates the blueprint schema if improved. Disabled by default. See `docs/discovery.md` — Blueprint Optimization section.

- **idp_common API Reference & Documentation** — Added `docs/idpcommon-api-reference.md` covering all 22 modules, created 6 missing module READMEs (discovery, schema, image, s3, utils, metrics), updated core data model docs to match current code, fixed `IDPConfig` lazy-loading bug in `__init__.py`, and integrated into docs-site sidebar.

- **Consolidated publish and headless deploy into `idp-cli`** — All build/publish/deploy functionality now available through the CLI, deprecating standalone scripts:
  - `publish.py` and `publish.sh` are deprecated — use `idp-cli publish` instead. `publish.py` remains as a thin backward-compatibility wrapper. `publish.sh` has been removed.
  - `scripts/generate_govcloud_template.py` is deprecated — use `idp-cli publish --headless` or `idp-cli deploy --headless` instead. The script remains as a thin wrapper.
  - New `--template-file` option on `idp-cli deploy` for deploying from a local CloudFormation template file produced by a previous `idp-cli publish`.
  - `idp-cli deploy --headless` (without `--from-code`) now downloads the published template, transforms to headless with GovCloud config defaults, uploads to S3, and deploys — all in one command.

### Fixed

- **HITL review start overwrites document sections** — Fixed the Start Review action to update only the Review Status and Review Owner fields, preserving all existing document sections and other fields.

- **Evaluation schema error for free-form objects** — Stickler mapper now detects and skips unevaluable object schemas (e.g., objects with `additionalProperties` but no defined `properties`, and arrays of such objects) instead of raising validation errors.

- **Full document reprocess not re-running OCR** — Fixed bug where clicking "Reprocess" in the UI reused stale OCR results from the previous run instead of re-executing OCR with the current configuration. The reprocess resolver now deletes previous output data from S3 before queuing, preventing the OCR function's retry-safe recovery from reinstalling old results.

- **Agentic extraction timeout on long documents** — Fixed repeated Lambda timeouts when agentic extraction exceeds the 15-minute limit on large documents (e.g., 25-page brokerage statements with 600+ holdings). Added incremental S3 checkpointing that saves extraction state after each tool call — covers both the extraction tools path (`extraction_tool`, `apply_json_patches`, `make_buffer_data_final_extraction`) and the buffer tools path (`patch_buffer_data`) that the agent uses for very large batched extractions. The checkpoint format tracks which state was saved (`current_extraction` vs `intermediate_extraction` buffer) so the correct resume path is used. On Step Function retry, the Lambda loads the checkpoint and the agent resumes from where it left off rather than restarting from scratch. No CloudFormation or Step Function changes required — the existing `Sandbox.Timedout` retry mechanism now makes incremental progress. Only active when agentic extraction is enabled; standard extraction is unaffected.

- **Agentic extraction fails on Bedrock InternalServerException without retrying** — Fixed `InternalServerException` errors (transient Bedrock server-side errors) causing immediate Lambda failure after only botocore's fast 7 retries, bypassing the application-level retry decorator (50 retries with 5s→1800s exponential backoff). Root cause: `InternalServerException` and `InternalServerError` were missing from all three retry layers — the `async_exponential_backoff_retry` decorator's `DEFAULT_RETRYABLE_ERRORS` set (`bedrock_utils.py`), the `BedrockClient._invoke_with_retry()` retryable errors list (`bedrock/client.py`), and the Step Functions ExtractionStep Retry `ErrorEquals` list (`workflow.asl.json`). All three layers now include these transient errors, providing proper exponential backoff retry at the application level and Lambda-level retry via Step Functions as a safety net.

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.5.4.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.5.4.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.5.4.yaml`

## [0.5.3]

### Added

- **Discovery UX Enhancements** — Major improvements to the Discovery experience:
  - **Multi-Section Package Discovery** — New "Multi-Section Package" discovery mode with PDF page thumbnail preview, color-coded page ranges, and parallel job creation. Users define page ranges to discover multiple classes from a single PDF. Each range creates an independent discovery job.
  - **✨ AI Auto-Detect Sections** — "Auto-detect sections" button uses a configurable LLM prompt (`discovery.auto_split`) to automatically identify document boundaries and pre-fill page ranges with document type labels.
  - **Discovery Mode Selector** — Tile-based mode choice between "Single Section Document" (with optional ground truth) and "Multi-Section Package" (with page ranges). Ground truth and page ranges are mutually exclusive.
  - **Class Name Hints** — Document type labels (from auto-detect or manual entry) are passed as class name hints to guide the discovery LLM's `$id` and `x-aws-idp-document-type` output.
  - **Real-time Job Monitoring** — Live progress messages, elapsed time counters, phased upload status ("Creating jobs..." → "Uploading..." → "Refreshing..."), discovered class name badges, and expandable error details with user-friendly messages.
  - **Jobs Table UX** — Search/filter, time range selector, pagination, resizable columns, column preferences, multi-select delete, config version hyperlinks, and page range badges on multi-section jobs.
  - **S3 Upload Race Condition Fix** — Replaced hardcoded `time.sleep(30)` with smart S3 polling using exponential backoff (2s–10s, 60s timeout).
  - **New GraphQL APIs** — `autoDetectSections` mutation, `pageRanges`/`pageLabels` on `uploadDiscoveryDocument`, `pageRange`/`discoveredClassName`/`statusMessage` on job types, `deleteDiscoveryJob` mutation.

- **Discovery CLI & SDK Enhancements** — New capabilities in `idp-cli discover` and `client.discovery` that bring parity with the Web UI's Discovery features:
  - **Class Name Hints** — `--class-hint` (CLI) / `class_name_hint=` (SDK) to pre-label discovered classes, guiding the LLM's `$id` output.
  - **Multi-Section Page Ranges** — `--page-range "1-3" --page-label "W2 Form"` (CLI, repeatable) / `discovery.run_multi_section(page_ranges=[...])` (SDK) to discover multiple document classes from a single multi-page PDF.
  - **AI Auto-Detect Sections** — `--auto-detect` / `--detect-only` (CLI) / `discovery.auto_detect_sections()` (SDK) to automatically identify document section boundaries using LLM analysis, then optionally discover each section.
  - **BDA Sync Command** — New `idp-cli config-sync-bda` command and `client.config.sync_bda()` SDK method for explicit bidirectional synchronization between IDP configuration classes and BDA blueprints. Supports `--direction` (bidirectional, bda-to-idp, idp-to-bda) and `--mode` (replace, merge).
  - **New Models** — `AutoDetectResult`, `AutoDetectSection`, `ConfigSyncBdaResult`, `page_range` field on `DiscoveryResult`.

- **IDP SDK & CLI Overhaul** — Major refactoring of the SDK and CLI for a cleaner, more maintainable architecture:
  - **`IDPClient` entry point** — Single public interface with typed namespace access (`client.batch`, `client.stack`, `client.config`, `client.manifest`, `client.testing`). CLI commands now route through `IDPClient` instead of importing internal modules, ensuring consistent behavior across CLI, Web UI, and programmatic access.
  - **Typed return models** — SDK operations return Pydantic models instead of raw dictionaries, enabling IDE auto-complete and type checking.
  - **Enhanced config validation** — Manifest and config validation reports deprecated/unknown fields; config upload detects whether a version exists and handles creation vs. update correctly.
  - **Enhanced stack operations** — Deploy and delete commands support in-progress detection, live monitoring, cancel-update, and failure analysis.
  - **Private API boundaries** — Internal modules renamed from `core/` to `_core/` with lint rules enforcing the boundary.

- **IDP MCP Connector** — Local package that bridges coding assistants like Cline and Kiro to the IDP MCP Server with automatic Cognito authentication and dynamic tool discovery.

- **ALB+S3 VPC Hosting Mode** — Alternative web UI hosting using Application Load Balancer with S3 VPC Interface Endpoint for environments that require VPC-based hosting (private networks, regulated environments, corporate networks without internet-facing CDN access). ([#245](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/pull/245))
  - New `WebUIHosting` parameter (`CloudFront` | `ALB`) with conditional resource creation — CloudFront and ALB resources are mutually exclusive
  - ALB hosting nested stack (`nested/alb-hosting/template.yaml`) with ALB, S3 Interface VPC Endpoint, security groups, custom resource Lambdas for VPC CIDR lookup and target registration
  - TLS 1.3 enforcement, access logging, scoped VPC endpoint policy (`s3:GetObject`/`s3:ListBucket` only), and multi-CIDR security group ingress management
  - Self-signed certificate generation script (`scripts/generate_self_signed_cert.sh`) for demo/testing
  - New documentation: `docs/alb-hosting.md` — prerequisites, deployment steps, security considerations, troubleshooting, CloudFront vs ALB comparison

- **`make help` target** — Added `make help` with categorized, auto-generated descriptions for all 33 Makefile targets; updated CONTRIBUTING.md to match.

- **Test Studio Field-Level Metrics** — Test results now display per-field extraction performance in an interactive table showing Field Name, Accuracy, Precision, Recall, TP, FP, TN, FN. Metrics are searchable, sortable, and paginated in an expandable section. Enables identification of low-performing fields and tracking improvements after configuration changes.

- **Stickler Bulk Aggregation for Test Studio** — Test Studio now uses Stickler's `BulkStructuredModelEvaluator` with `aggregate_from_comparisons()` for accurate metric aggregation across multiple documents. Each document is evaluated with `include_confusion_matrix=True`, results are stored in S3, and aggregated when viewing test results. Eliminates Athena queries for new data, improving accuracy, consistency, and cost-effectiveness.

- **RBAC Security Hardening** — Comprehensive audit and hardening of GraphQL API authorization against the documented RBAC permission matrix:
  - **Query-level `@aws_auth` directives** — Added server-side role enforcement to 20+ GraphQL queries that were previously open to all authenticated users. Configuration, pricing, capacity, discovery, test studio, config library, and agent query system queries now enforce role restrictions at the AppSync schema level (e.g., Reviewer cannot access configuration, discovery, test studio, or pricing queries).
  - **Admin-only enforcement for "Save as Version" / "Save as Default"** — The `updateConfiguration` resolver now checks caller role and rejects non-Admin users attempting `saveAsVersion` or `saveAsDefault` operations, which were previously only blocked in the UI.
  - **Server-side RBAC filtering in `listDocumentsByDateRange`** — Added reviewer-only document filtering and config-version scope filtering to the date range resolver, matching the existing `listDocuments` GSI resolver pattern. Updated CloudFormation template with `USERS_TABLE_NAME` environment variable and DynamoDB IAM permissions.
  - **Updated RBAC documentation** (`docs/rbac.md`) — Complete mutation and query authorization tables, AppSync `@aws_auth` + `@aws_iam` limitation documented, all previously missing API entries added.

- **Threat Model Documentation** — Comprehensive threat model for the GenAI IDP Accelerator covering architecture overview, STRIDE analysis, feature-specific threats (agent analysis, companion chat, knowledge base, Lambda hooks, MCP integration, RBAC, reporting, SDK/CLI, web UI), risk assessment matrix, AI-generated threat analysis, implementation guide, and Threat Composer JSON export.

- **Managed Configuration Versions** — Pre-deployed test sets now have dedicated stack-managed config versions (`managed: true`) that are automatically created and overwritten on stack updates. Save and delete are disabled for managed versions in the UI and API. Test Studio auto-selects the matching config version when a test set is selected, replacing the hardcoded mapping.

- **Removed older Claude models** from Configuration UI picklists (3.x, 4.0, 4.1). Haiku 4.5, Sonnet 4.5, Sonnet 4.6, Opus 4.5, and Opus 4.6 are available for selection in the UI. Existing configurations using older versions still work.

### Changed

- **SDK & CLI: Renamed processing commands for clarity** — Old names are deprecated (emit `DeprecationWarning`) but remain available for backward compatibility:
  - `client.batch.run()` → `client.batch.process()`
  - `client.batch.rerun()` → `client.batch.reprocess()` (same for `client.document.rerun()` → `.reprocess()`)
  - `idp-cli run-inference` → `idp-cli process`
  - `idp-cli rerun-inference` → `idp-cli reprocess`
- **SDK: `stack.delete()` now waits by default** — The `wait` parameter defaults to `True` (previously fire-and-forget). Pass `wait=False` to restore the old behavior.
- **MCP: Renamed `docs/mcp-integration.md` to `docs/mcp-server.md`** for clarity.
- **MCP: Renamed Lambda function `agentcore_analytics_processor` to `agentcore_mcp_handler`** to better reflect its role as the MCP protocol handler (not just analytics).
  - CloudFormation resource `AgentCoreAnalyticsLambdaFunction` → `AgentCoreMCPHandlerFunction`
  - CloudFormation resource `AgentCoreAnalyticsLambdaLogGroup` → `AgentCoreMCPHandlerLogGroup`
  - Lambda FunctionName: `${StackName}-agentcore-analytics` → `${StackName}-agentcore-mcp-handler`
  - Source directory: `src/lambda/agentcore_analytics_processor/` → `src/lambda/agentcore_mcp_handler/`

- **Page images broken for document IDs containing parentheses** — Fixed issue where document page thumbnails and Visual Document Editor images failed to load (showing "Image load error") when the document ID contained parentheses (e.g., `lending_package(1).pdf`). Root cause: JavaScript's `encodeURIComponent()` does not encode `(`, `)`, `!`, `'`, `*` but AWS S3 SigV4 requires them to be percent-encoded in the canonical URI, causing signature mismatches. Added S3-safe URI encoding in `generate-s3-presigned-url.ts`.
- **"View Rule Validation Summary" button not appearing in real-time** — Fixed two-part bug: (1) State machine `ResultPath` for rule validation steps wrote to `$.RuleValidationOrchestrationResult` instead of `$.Result`, so downstream steps lost the `rule_validation_result`. (2) `RuleValidationResultUri` was missing from `UPDATE_DOCUMENT` and `GET_DOCUMENT` GraphQL selection sets in `mutations.py`, so AppSync subscriptions never delivered the field to the UI. Button appeared only after page refresh.
- **Page images broken for document IDs containing parentheses** — Fixed issue where document page thumbnails and Visual Document Editor images failed to load (showing "Image load error") when the document ID contained parentheses (e.g., `lending_package(1).pdf`). Root cause: JavaScript's `encodeURIComponent()` does not encode `(`, `)`, `!`, `'`, `*` but AWS S3 SigV4 requires them to be percent-encoded in the canonical URI, causing signature mismatches. Added S3-safe URI encoding in `generate-s3-presigned-url.ts`.
- **"View Rule Validation Summary" button not appearing in real-time** — Fixed two-part bug: (1) State machine `ResultPath` for rule validation steps wrote to `$.RuleValidationOrchestrationResult` instead of `$.Result`, so downstream steps lost the `rule_validation_result`. (2) `RuleValidationResultUri` was missing from `UPDATE_DOCUMENT` and `GET_DOCUMENT` GraphQL selection sets in `mutations.py`, so AppSync subscriptions never delivered the field to the UI. Button appeared only after page refresh.
- **Fillable PDF form fields missing from rendered page images** — Fixed bug where fillable PDF form fields (text inputs, checkboxes, radio buttons, dropdowns) were not rendered in page images, causing OCR and extraction to miss user-entered data. Two-part fix: (1) `PdfDocument.init_forms()` initializes the form rendering engine so PDFium can process form fields, and (2) `page.flatten()` merges form field appearances into page content before rendering — required because many fillable PDFs (especially government forms) lack pre-generated appearance streams. Applied in both Pattern 2 (`OcrService`) and Pattern 1 (`create_pdf_page_images`) PDF rendering pipelines. ([#240](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/240))
- **CLI monitoring exits prematurely before documents start processing** — Fixed bug where `idp-cli process --monitor` would exit immediately after uploading documents, showing 0% completion even though documents were still queued. Root cause: The monitoring loop checked `all_complete` which returned `True` when no documents had completed or failed yet (0 == 0). Added 60-second grace period before allowing early exit, ensuring monitoring waits for documents to be picked up by the queue and start processing.
- **Discovery subscription handler dropping errorMessage and other fields** — Fixed bug where the UI subscription handler did `{ ...oldJob, status: updatedJob.status }`, discarding all fields except status from real-time subscription updates. Error messages, discovered class names, and status messages were being sent by the backend but silently dropped by the UI. Now spreads all fields: `{ ...oldJob, ...updatedJob }`.
- **Discovery processor S3 race condition causing NoSuchKey failures** — The discovery upload resolver sends the SQS message before the browser finishes uploading the file to S3 via presigned POST. Previously worked around with a hardcoded `time.sleep(30)`. Replaced with `_wait_for_s3_object()` that polls S3 with exponential backoff (2s initial, 10s max, 60s timeout), proceeding as soon as the file appears.
- **CLI `--parameters` parsing for comma-delimited values** — Fixed `idp-cli deploy --parameters` to handle values containing commas (e.g., `ALBSubnetIds=subnet-a,subnet-b`). Previously the naive `split(",")` broke multi-value parameters. ([#245](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/pull/245))
- **GovCloud template: fix unresolved RBAC resource dependencies** — Added `AuthorGroup`, `ViewerGroup`, `GetMyProfileResolver`, and `UpdateUserResolver` to GovCloud removal lists so they are stripped alongside the `UserPool` they depend on.
- **Document status and sections not updating in real-time during processing** — Fixed regression from RBAC commit where `updateDocumentStatus` subscription events (used during Map state steps: Extraction, Assessment, Rule Validation) were silently discarded because they lacked `InitialEventTime`/`QueuedTime` fields, causing `isDocumentInActiveRange` to reject them. Also fixed: stale sections/pages not clearing immediately on full reprocess, sections appearing duplicated or out of order during parallel Map execution (null-protection merge + client-side sort by page ID).
- **Fix race condition in `idp-cli generate-manifest --test-set`** — Added `.uploading` marker file protocol to prevent the test set resolver from prematurely validating test sets while the CLI is still uploading baseline files ([#193](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/193)).
- **Test Fixes** — Updated CLI test mocks to align with the new `IDPClient`-based implementation, fixing broken test fixtures that referenced removed internal imports.

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.5.3.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.5.3.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.5.3.yaml`

## [0.5.2]

### Added

- **Multi-tenancy with Role-Based Access Control (RBAC)** — 4-role model (Admin, Author, Reviewer, Viewer) with server-side AppSync auth directives, server-side Reviewer document filtering, and UI adaptation. Admin has full access; Author can edit config and process documents but cannot manage users or delete config versions; Viewer has read-only access (editors, save buttons, and edit mode all disabled); Reviewer sees only HITL-pending documents. Non-admin roles can be scoped to specific use cases via `allowedConfigVersions`. See `docs/rbac.md`.

- **Standard Class Catalog** — When adding a new document class in the Schema Builder, users can now choose between **Custom Class** (define from scratch) and **Standard Class** (import from a catalog of 35 pre-built document types). Standard classes are derived from AWS BDA standard blueprints and include common document types like Invoice, Receipt, W-2, Bank Statement, Payslip, US Driver License, US Passport, various tax forms (1040, 941, 940, W-9, 1098, 1099), insurance cards, birth/death/marriage certificates, and more. Each standard class comes with a complete extraction schema including attributes, descriptions, and nested types. Imported classes are fully editable. Run `make classes-from-bda` to refresh the catalog from the BDA API.

- **Documentation Site** — Added a hosted documentation site built with [Astro Starlight](https://starlight.astro.build/), auto-deployed to GitHub Pages. Provides full-text search (Pagefind), sidebar navigation organized by topic, dark/light mode, and a professional landing page — all sourced directly from the existing `docs/` markdown files with zero content duplication. Browse at [aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws](https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/).

- **Discovery accessible from CLI and SDK** — Discovery can now be run programmatically via the IDP SDK (`client.discovery.run()`) and CLI (`idp-cli discover`), enabling users with many document classes to automate schema generation without the Web UI. Supports both modes: without ground truth (exploratory) and with ground truth (optimized). ([#228](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/228))

- **Custom Model Fine-tuning** — Improve extraction and classification accuracy for your specific document types by fine-tuning Amazon Nova models on your own labeled data — no ML expertise required. Select a Test Set with ground truth, choose a base model, and the system handles training data generation, Bedrock fine-tuning, and on-demand model deployment automatically. Custom models are billed pay-per-token with no idle costs. Available to Admin and Author roles. See [Custom Model Fine-tuning](./docs/custom-model-finetuning.md) for details.
  - **Web UI**: New "Custom Models" page with job creation form (test set selector, base model selector, train/validation split), jobs table with status tracking, and detailed job view with deployment status and configuration version creation
  - **CLI / SDK**: `idp-cli finetuning create`, `idp-cli finetuning status`, `idp-cli finetuning list`, `idp-cli finetuning delete` commands for programmatic job management
  - **GraphQL API**: New `createFinetuningJob`, `getFinetuningJob`, `listFinetuningJobs`, `deleteFinetuningJob` mutations/queries with `FinetuningJob` type and real-time status fields
  - **Step Functions Workflow**: 7-Lambda orchestration pipeline — list documents, parallel document processing (Distributed Map), merge training data, create Bedrock fine-tuning job, poll job status, deploy custom model via Provisioned Throughput
  - **CloudFormation Resources**: `FinetuningDataBucket` (S3), `FinetuningStateMachine` (Step Functions), 7 Lambda functions with IAM roles, CloudWatch log groups, and Bedrock permissions for model customization and deployment
  - **Shared Training Data Utilities**: Common module (`idp_common.model_finetuning.training_data_utils`) for extraction field parsing, baseline formatting, PDF-to-image conversion, and document image handling — shared across Lambda functions to eliminate code duplication

### Changed

- **Python 3.12+ now required** — Updated minimum Python version from 3.10 to 3.12 to address security vulnerabilities in transitive dependencies.

- **Sync to BDA no longer auto-activates the config version** — Previously, performing "Sync to BDA" would automatically set the current config version as active. Since each config version now has its own BDA project, auto-activation is unnecessary. Users can manually choose which version to activate via the Versions table. The "Sync to BDA" confirmation modal text has been updated accordingly.

- **Removed `Bedrock Data Automation (BDA) Project ARN` CloudFormation parameter** — The deploy-time `Pattern1BDAProjectArn` parameter has been removed as it was redundant with the per-config-version BDA project management already available in the Web UI, CLI, and GraphQL API. BDA projects are now managed entirely post-deployment: enable `use_bda: true` in your configuration, then use "Sync to BDA" to create or link a BDA project, or "Sync from BDA" to import from any existing BDA project. This simplifies the deployment experience (one fewer parameter) and better aligns the CloudFormation interface with the system's actual architecture. Existing deployed stacks are unaffected — runtime BDA project ARN resolution reads from DynamoDB per-version tracking, not from the CloudFormation parameter. Also removed the unused `nested/bda-lending-project/` directory (dead code not referenced by any template) and the legacy `BDA_PROJECT_ARN` environment variable fallback from the sync resolver.

### Fixed

- **CLI: Remove deprecated `--pattern` references** — Updated `idp-cli.md` and CLI code to reflect the unified pattern architecture. Removed `--pattern` from all deploy and config command examples/options.

- **Discovery no longer injects default config classes into target version** — Previously, running Discovery on a configuration version would merge all classes from the `default` version into the target version alongside the newly discovered class. Now Discovery only adds/updates the discovered class within the target version's own class list, keeping the version's classes exactly as the user curated them.

- **Documentation: Comprehensive review and cleanup** — Fixed outdated references, broken links, and missing content across documentation files.

- **Inference Profile pricing ARN truncation in UI** — Fixed pricing display and cost breakdown truncation for Bedrock Application Inference Profile ARNs containing multiple `/` characters (e.g., `bedrock/arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/088k6ehrxpci`). The UI was splitting on all `/` separators instead of preserving the full ARN, causing the profile ID to be dropped in the Pricing page display, Test Studio cost breakdowns, and CSV exports. Backend pricing lookup was not affected. ([#237](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/237))

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.5.2.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.5.2.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.5.2.yaml`

## [0.5.1]

### Added

- **Scalable Document List and Test Executions** — Comprehensive redesign to eliminate UI and backend bottlenecks when working with thousands of documents. ([#203](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/203))
  - **TypeDateIndex GSI on TrackingTable**: New DynamoDB Global Secondary Index (`ItemType` + `InitialEventTime`) enables efficient queries by item type (document, testrun, testset) sorted by time, replacing full table scans. Includes 20 projected attributes for list-view rendering without base table fetches.
  - **GSI Attribute Backfill Mechanism**: Robust Step Functions state machine with parallel scan workers that automatically backfills `ItemType` and `HITLPendingReview` attributes on existing items during stack upgrades. Features timeout-safe continuation, idempotent conditional updates, and automatic trigger via CloudFormation Custom Resource.
  - **GSI-Based Document List Resolver**: New `listDocuments` Lambda resolver queries the TypeDateIndex GSI with server-side pagination (`limit`/`nextToken`).
  - **`getDocumentCount` API**: New efficient count query using GSI `Select: 'COUNT'` for accurate document totals without fetching data.
  - **UI Document List Rewrite**: Eliminated the N+1 query pattern (shard queries → individual `getDocument` per document). Now uses a single paginated `listDocuments` GSI query for all time periods. First page renders immediately with incremental background loading of remaining pages.
  - **Subscription Optimization**: `onUpdateDocument` events now use subscription data directly instead of triggering individual `getDocument` API calls, eliminating thousands of redundant requests during active processing.
  - **GSI-Based Test Runs Query**: Replaced full table scan in `get_test_runs()` and `get_test_runs_by_date_range()` with GSI query + BatchGetItem pattern for efficient test run listing with all fields (including Context, ConfigVersion).
  - **GSI-Based Test Sets Query**: Replaced full table scan in `get_test_sets()` with GSI query + BatchGetItem pattern, avoiding scanning the entire TrackingTable (which includes all documents) just to find ~10 test sets.
  - **`ItemType` Written on All Creation Paths**: All document, test run, and test set creation paths (DynamoDB service, AppSync resolvers, test runners, dataset deployers) now write `ItemType` and `InitialEventTime` for immediate GSI indexing.
  - **Improved Error Messages**: Document list errors now show the actual failure reason (e.g., Lambda throttling, timeout details) instead of generic "please try again" messages.

- **GraphQL Type Generation & Unit Testing** — Replaced 60+ hand-written GraphQL query/mutation/subscription files with auto-generated types via `@graphql-codegen`, added typed AWSJSON parsers with unit tests (vitest + jsdom), and integrated a CI codegen-check to prevent type drift.

- **Third-Party Model Support** — Added Meta Llama 4 Maverick 17B, Llama 4 Scout 17B, Google Gemma 3 27B IT, and NVIDIA Nemotron Nano 12B v2 VL as selectable models across all pipeline stages (OCR, Classification, Extraction, Assessment, Summarization, Evaluation, Discovery, Agents, Rule Validation). Includes per-token pricing configuration and EU region fallback mappings for Llama 4 models. ([#217](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/217))

- **Load Test Config Version Support** — Added `--config-version` parameter to the `idp-cli load-test` command, enabling load tests to target a specific configuration version. Files uploaded during load tests now include `config-version` S3 metadata, consistent with the `process` command behavior.

- **Deploy Failure Root Cause Analysis** — Enhanced `idp-cli deploy` failure reporting to recursively analyze nested stack events and identify actual root causes. Previously, failures in nested stacks showed only a generic "Embedded stack was not successfully created" message. Now displays a structured "Root Cause Analysis" section with the specific resource, type, and error message from the nested stack that caused the failure, along with cascade failure counts.

- **MCP Server** — Added additional tool to MCP Server for retrieving results of the processed document from the IDP system.

### Changed

- **OCR Benchmark Config Optimization** — Optimized `config_library/unified/ocr-benchmark` configuration with targeted field descriptions, explicit model/prompt/OCR settings, and corrected date format (YYYY-MM-DD to match ground truth). Improved overall extraction accuracy from 51.5% to 75.2% on the full 293-document benchmark at equivalent cost (~$2.62). Classification remains 100% across all 9 document classes. ([#220](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/pull/220))

- **GraphQL Type Generation & Unit Testing** — Replaced 60+ hand-written GraphQL query/mutation/subscription files with auto-generated types via `@graphql-codegen`, added typed AWSJSON parsers with unit tests (vitest + jsdom), and integrated a CI codegen-check to prevent type drift.

### Fixed

- **AgentCore Gateway Manager** — Fixed the issue where gateway was not getting deleted once stack is deleted.

- **Configuration Page Error Display** — Fixed `[object Object]` error message when configuration loading fails (e.g., due to Lambda throttling) by properly extracting error messages from Amplify GraphQL error responses.

- **OCR Retry Logic** — Fixed broken retry chain between OCR Lambda and Step Functions that caused document processing failures under Textract throttling. The OCR Lambda was catching `ProvisionedThroughputExceededException` and re-raising it as a generic `Exception`, which Step Functions didn't match for retries. Now propagates a `ThrottlingException` that Step Functions can retry on. Also added retry-safe page skipping so retries only re-process failed pages instead of re-OCRing the entire document, and increased OCR step retry attempts from 2 to 6 with longer backoff intervals. ([#195](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/195))

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.5.1.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.5.1.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.5.1.yaml`

## [0.5.0]

### Added

- **Unified Pattern** — Merged Pattern-1 (BDA) and Pattern-2 (Pipeline) into a single deployment. Switch between BDA and Pipeline processing modes at runtime using the `use_bda` configuration toggle — no redeployment needed. Use [Test Studio](./docs/test-studio.md) to compare accuracy and cost across both modes to find the optimal approach for your documents. See the [Migration Guide](./docs/migration-v04-to-v05.md) for upgrade instructions.

- **Rule Validation for BDA mode** — Rule validation (business rule checking) is now available in both BDA and Pipeline modes. Previously it was Pipeline-only.

- **Fake W-2 Tax Form Test Set Auto-Deployment** — New pre-deployed benchmark test set with 2,000 synthetically generated US W-2 tax form images and structured ground truth, sourced from HuggingFace (`singhsays/fake-w2-us-tax-form-dataset`, originally from Kaggle under CC0: Public Domain license). Features 45 ground truth fields per document covering employer info (EIN, name, address), employee info (SSN, name, address), federal wages/taxes (boxes 1-8), compensation codes (boxes 12a-d), checkboxes (box 13), and state/local taxes (boxes 15-20). Includes both clean and noisy image variants for testing OCR robustness. Ideal for benchmarking W-2 extraction accuracy, evaluating image quality impact on processing, and testing structured form data extraction at scale.

- **AWS Profile Support for CLI** — Added optional `--profile` parameter to specify AWS credentials profile. Can be placed anywhere in the command. Automatically applies to all AWS SDK calls.

- **Enhanced `status` CLI/MCP Command with Advanced Search, Filtering, and Analytics** — Added PK substring search (`--batch-id` now matches partial batch identifiers across multiple batches), `--object-status` filter for searching by processing status (COMPLETED, FAILED, etc.), `--get-time` flag for timing statistics (processing, queue, total time with min/max outlier tracking), `--include-metering` flag for Lambda GB-seconds usage and cost estimates, and `--show-details` flag for detailed document information. Introduces `TrackingTableSearcher` class for flexible DynamoDB tracking table queries. Fully backward compatible with existing usage.

- **Added Replace/Merge sync modes for BDA synchronization** — Both "Sync from BDA" and "Sync to BDA" now support two modes: **Replace** (default) aligns the target to match the source exactly, removing items not in the source; **Merge** adds source items to the target without removing existing items. The UI modal now always shows a mode selection and ARN input (pre-filled for linked projects).

### Deprecated

- **Pattern-1 (BDA) and Pattern-2 (Pipeline) separate deployments** — Replaced by the Unified Pattern. Existing stacks are automatically upgraded. See the [Migration Guide](./docs/migration-v04-to-v05.md) for details.

- **Pattern-3 (UDOP + Bedrock)** — Pattern-3 is no longer available as a deployment option. If you are currently using Pattern-3 with a SageMaker UDOP endpoint, do not upgrade to v0.5.x without first testing in a non-production environment. You can use the [Lambda Inference Hooks](./docs/lambda-hook-inference.md) feature (introduced in v0.4.15) to call your existing SageMaker UDOP endpoint from the unified pattern's classification step via a custom Lambda function.

### Changed

- **Switched `idp_sdk` pyproject.toml to auto-discovery** — Replaced explicit subpackage listing with `setuptools.packages.find` using `include = ["idp_sdk*"]` so new subpackages are automatically included without manual pyproject.toml updates.

- **Resilient Test Set Deployment — Graceful Degradation on Download Failures** — All test set deployer Lambdas (RealKIE-FCC, OmniAI-OCR-Benchmark, DocSplit-Poly-Seq) now handle download failures gracefully instead of causing CloudFormation stack rollbacks. When a dataset source (HuggingFace) is unreachable or a download fails, the deployer creates a FAILED test set record in DynamoDB with a descriptive error message visible in the Test Studio UI, and sends `cfnresponse.SUCCESS` to CloudFormation so the stack deployment continues. Previously failed deployments are automatically retried on the next stack update. This ensures transient third-party service outages never block IDP infrastructure deployment.

- **Replaced PyMuPDF (AGPL-3.0) with pypdfium2 (Apache-2.0/BSD-3-Clause) for PDF rendering** — Resolves license incompatibility with the project's MIT-0 license. pypdfium2 provides equivalent PDF-to-image rendering using PDFium engine. Page rendering is now performed sequentially before parallel OCR processing to ensure thread-safety.

### Fixed

- **Fixed "Sync from BDA" not removing IDP classes absent from BDA project** — Previously, "Sync from BDA" only added new classes from the BDA project without removing classes that weren't in BDA. Now defaults to "Replace" mode which fully aligns the config version's classes with the BDA project, removing classes not present in BDA. A new "Merge" mode is also available to preserve the legacy additive behavior.

- **Fixed insufficient Lambda memory for Extraction, Assessment, and Evaluation functions in unified pattern template** — Increased MemorySize from 512 MB (Extraction, Assessment) and 1024 MB (Evaluation) to 4096 MB to match all other document processing Lambda functions, preventing potential out-of-memory errors during document processing. ([#205](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/205))

- **Fixed DOCX processing to extract text from embedded images and correct page splitting** — DOCX files with embedded images (e.g., `<w:drawing>` elements) now have image content OCR'd and included in the extracted text instead of being silently skipped. Page splitting now uses DOCX metadata (explicit page breaks, image display dimensions from `wp:extent`, section properties) instead of inaccurate height estimates, producing correct page boundaries.

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.5.0.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.5.0.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.5.0.yaml`

## [0.4.16]

### Added

- **Capacity Planning (Beta - Pattern 2 Only)**
  - Comprehensive capacity planning tool to optimize document processing performance, predict resource requirements, and calculate AWS service quota needs
  - **Pattern 2 Exclusive**: Only available for Pattern 2 deployments
  - **Token Usage Configuration**: Define expected tokens per document type for each processing step (OCR, Classification, Extraction, Assessment, Summarization)
  - **Auto-Populate from Documents**: Extract token usage and page counts from actual processed documents' metering data with time range filtering (2hrs to 30 days or custom date range)
  - **Processing Schedule**: Configure hourly document volumes with template-based auto-fill options (single slot, all doc types at 9 AM, business hours, full day)
  - **Quota Calculation**: Automated AWS Bedrock quota requirements (TPM and RPM) with 10% safety buffer
  - **Export Capabilities**: Complete capacity plan export with configuration version, model details, token usage, schedule, and quota requirements
  - **GitHub Feedback Integration**: Beta feature with direct link to GitHub Issues for user feedback
  - **Documentation**: New [capacity-planning.md](docs/capacity-planning.md) with comprehensive feature guide, calculation formulas, and safety buffer explanations
- **React UI TypeScript Migration (Phases 1–3 — Complete)** — Completed full migration of the React UI codebase from JSX/JS to TSX/TS. Phases 1–2 added TypeScript tooling and migrated contexts, hooks, constants, and utilities. Phase 3 migrated all remaining 207 components, GraphQL operations, routes, modals, and entry points across 13 incremental sub-phases (211 files changed). Removed `prop-types` dependency; all runtime prop validation replaced with TypeScript interfaces. No behavioral or visual changes. ([#187](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/187), [#188](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/pull/188), [#191](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/pull/191), [#198](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/pull/198))
- **Configuration Version Management Commands for CLI and SDK** — Added `config-list`, `config-activate`, and `config-delete` CLI commands and corresponding `client.config.list()`, `client.config.activate()`, `client.config.delete()` SDK operations for programmatic configuration version management. Includes safety protections (default/active version deletion prevention, confirmation prompts, existence validation), `--force` flag for automation, and Rich table output for version listing.
- **Added support for Claude Opus 4.6 model and Long Context (1M) variant**
- **Added support for Claude Sonnet 4.6 model and Long Context (1M) variant**
- **Included MCP tools `process`, `reprocess`, `status`, `search` for document processing**
- **Added `process` and `reprocess` CLI commands for batch operations via command line**
- **Added external mcp client example `examples/external-mcp-client`**
- **Maintained `run-inference` and `rerun-inference` CLI commands with deprecation notices**

### Fixed

- **Fixed DynamoDB 400KB item size limit blocking configs with 45+ document classes** — Configuration data is now gzip-compressed before storing to DynamoDB, achieving 37-95x compression ratios. Supports 3,000+ document classes within the 400KB limit. Fully backward compatible with existing deployments. ([#200](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/200), [#201](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/pull/201))
- **Fixed Processing Flow chart using active stack config instead of the document's actual config version** for determining disabled steps (assessment, summarization, etc.)
- **Fixed `idp_sdk` pip install from GitHub missing subpackages** — Non-editable pip installs of `idp_sdk` from GitHub were missing `core/`, `models/`, and `operations/` subpackages, causing `ModuleNotFoundError`. Fixed by explicitly declaring all subpackages in `pyproject.toml`. ([#196](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/196))

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.4.16.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.4.16.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.4.16.yaml`

## [0.4.15]

### Added

- **Lambda Hook Inference (Custom LLM Integration)**
  - Customers can provide their own custom Lambda function to integrate with any LLM — models hosted on SageMaker, ECS, EC2, or external APIs — by selecting `LambdaHook` as the model in any pipeline step
  - **Per-Step Granularity**: Configure LambdaHook independently for OCR, Classification, Extraction, Assessment, and Summarization (Pattern-2)
  - **Converse API-Compatible Contract**: Lambda receives the same Converse API payload structure used with Bedrock, and returns a Converse API-compatible response — documented request/response format for easy implementation
  - **S3 Image References**: Inline image bytes automatically uploaded to S3 and replaced with `s3Location` references to avoid Lambda's 6MB payload limit
  - **GENAIIDP- Naming Convention**: Lambda function names must start with `GENAIIDP-` for secure, scoped IAM permissions
  - **Built-in Retry Logic**: Exponential backoff with jitter for transient errors (throttling, timeouts), matching Bedrock retry behavior
  - **Metering Integration**: Token usage from Lambda response tracked in document metering data for cost calculations
  - **Sample Functions**: Examples in `samples/lambda-hook-inference/` — Bedrock proxy (with customization points) and SageMaker endpoint hook, with SAM template
  - **Documentation**: New [lambda-hook-inference.md](docs/lambda-hook-inference.md) with architecture diagram, configuration guide, payload contract, SageMaker example, IAM, and limitations

- **Configuration Versioning System**
  - Manage multiple named configuration versions as complete, self-contained snapshots
  - **Version Management UI**: Configuration Versions table with create, compare, activate, delete, and import operations; version comparison with CSV/JSON export
  - **Full Config Storage**: Each version stores the complete configuration; editing and saving a version persists the full config, making behavior predictable and debuggable
  - **Active Version**: One version is marked active for new document processing; selectable when uploading documents, running tests, or reprocessing
  - **Version Tracking**: Config version recorded per document (S3 metadata + DynamoDB) and displayed across Document List, Document Details, Test Studio results, and all exports
  - **Unsaved Changes Protection**: Per-field unsaved change indicators (orange dots), info banner with "Discard changes" button, and browser navigation guards (`beforeunload` + SPA hash navigation)
  - **CLI Integration**: `--config-version` parameter for `run-inference`, `config-download`, and `config-upload` commands with version validation before processing
  - **Test Studio Integration**: Version selector in Test Runner, version tracking per test run, version displayed in Test Results and Test Comparison views
  - **Legacy Support**: Existing sparse-delta configs auto-detected and seamlessly migrated to full format on first read
  - **Stack Upgrade Independence**: Stack upgrades update only the `default` version; user versions are locked snapshots that users explicitly manage
  - **Documentation**: New [configuration-versions.md](docs/configuration-versions.md) with comprehensive feature documentation
  - **~200 lines of merge/delta/sync code removed**: Eliminated runtime merge logic, auto-sync on default updates, null-as-deletion semantics, and auto-cleanup of matching defaults

- **Custom Date Range Selector for Document List and Test Executions** - [GitHub Issue #177](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/177)
  - Added "Custom range..." option to the time period dropdown in both Document List and Test Studio → Test Results
  - Users can now select absolute start/end dates to query historical documents beyond the previous 30-day limit
  - **Scalable Server-Side Architecture**: Custom date ranges use a new `listDocumentsByDateRange` Lambda resolver that iterates shards server-side and batch-fetches documents, avoiding the client-side fan-out scalability issue
  - **Existing Behavior Preserved**: Relative period presets (2h through 30d) continue using the proven client-side shard mechanism — zero changes to existing code paths
  - **365-Day Maximum**: Date range capped at 365 days in the UI to prevent unbounded queries

### Fixed

- **Schema Builder Few-Shot Examples Input Focus Loss** - [GitHub Issue #174](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/174)
  - Fixed cursor jumping out of input fields after each keystroke when editing few-shot examples in the Schema Builder

- **Code Intelligence Agent - DeepWiki MCP Transport Migration**
  - Fixed "client initialization failed" error when using Code Intelligence Agent in Agent Companion Chat
  - **Root Cause**: DeepWiki deprecated their SSE transport endpoint (`/sse`) and now returns HTTP 410 Gone
  - **Solution**: Migrated from SSE (`sse_client`) to Streamable HTTP (`streamablehttp_client`) transport using the new `/mcp` endpoint
  - See DeepWiki documentation: https://docs.devin.ai/work-with-devin/deepwiki-mcp

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.4.15.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.4.15.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.4.15.yaml`

## [0.4.14]

### Added

- **Enhanced BDA to IDP Sync for Pattern-1**
  - Separate "Sync from BDA" and "Sync to BDA" buttons in the UI for explicit directional control instead of bidirectional-only sync
  - Parallel blueprint processing for improved sync performance on configurations with many document classes
  - Orphaned blueprint cleanup automatically detects and removes BDA blueprints no longer defined in IDP configuration
  - Warning notifications for skipped properties due to BDA limitations (nested arrays/objects), with guidance to flatten schemas using top-level `$defs`
  - AWS standard blueprint filtering prevents unintended modifications to AWS-managed blueprints

- **Human-in-the-Loop (HITL) Review Workflow Improvements**
  - **Review Ownership Model**: Reviewers must now claim documents using "Start Review" before editing, preventing concurrent edits
  - **Review In Progress Status**: New status displayed when a reviewer has claimed a document
  - **Filtered Document List for Reviewers**: Reviewers now see only documents pending review or their own in-progress reviews
  - **Admin Skip All Reviews**: Admins can skip all remaining section reviews without triggering document reprocessing
  - **Release Review**: Reviewers can release claimed documents back to pending status; Admins can release any review
  - **Review Completed By Field**: New column showing who completed or skipped the review (renamed from "Reviewed By")

- **Pattern-1 Edit Mode with Data-Only Editing and Reprocessing**
  - Added Edit Mode capability for Pattern-1 (BDA) stacks, enabling users to edit extraction data without modifying section structure
  - **Data-Only Editing**: Click "Edit Mode" then use "Edit Data" buttons on each section to open the Visual Editor for modifying predictions and ground truth
  - **Reprocessing Without BDA**: "Save and Reprocess" triggers evaluation and summarization steps without re-invoking Bedrock Data Automation (BDA)
  - **Section Structure Protection**: Section structure (IDs, classes, page assignments) remains read-only as managed by BDA blueprints
  - **Skip Logic Implementation**: State machine automatically detects existing pages/sections data and bypasses BDA invocation for reprocessing scenarios
  - **Use Cases**: Correct extraction errors, add baseline data for evaluation comparison, re-run evaluation after data corrections, update document summaries

### Changed

- **HITL Decoupled from Step Functions**: HITL review operations now update document status directly in DynamoDB without triggering workflow reprocessing, improving reliability and reducing unintended side effects

- **Renamed TestSet from RVL-CDIP-N-MP to DocSplit-Poly-Seq**
  - Updated Test Studio test set name to better reflect its purpose as a document splitting and classification benchmark
  - The underlying HuggingFace dataset source (`jordyvl/rvl_cdip_n_mp`) remains unchanged

- **Review Status Labels**: Renamed status values for consistency:
  - "Pending Review" → "Review Pending"
  - "Reviewed By" column → "Review Completed By"

### Fixed

- **HITL Decimal Serialization Error**: Fixed "Object of type Decimal is not JSON serializable" error when performing HITL operations (Start Review, Release Review, Skip All Reviews) by properly converting DynamoDB Decimal types

- **HITL Operations Clearing Estimated Cost**: Fixed issue where Start Review and Release Review operations were inadvertently clearing the Metering/Estimated Cost data by re-serializing the entire document; operations now update only HITL-specific fields

- **Pattern-1 Page/Section Number Alignment with Pattern-2 and Ground Truth**
  - Fixed page and section numbering mismatch between Pattern-1 (BDA) and Pattern-2 that caused evaluation failures when using shared test sets
  - **Root Cause**: BDA outputs 0-based indices while Pattern-2 and ground truth test sets use 1-based page IDs
  - **Solution**: Pattern-1 postprocessing now transforms S3 paths and Document model IDs to 1-based (`pages/1/`, `sections/1/`, `page_ids: ["1", "2"]`) while preserving 0-based `page_indices` arrays in result.json for internal consistency
  - **Key Distinction**: `page_indices` (array indices) remain 0-based, `page_id`/`section_id` (identifiers) are now 1-based
  - Both patterns now align correctly for evaluation with shared test sets and ground truth data

- **TIFF Image Format Support for Bedrock-Compatible Processing**
  - Fixed classification failure when processing TIFF image files ("Unsupported image format: TIFF")
  - OCR step now converts non-Bedrock-compatible formats (TIFF, BMP) to JPEG during page image extraction
  - Multi-page TIFF files handled like PDFs - each page becomes a separate document page

- **Discovery Feature Overwriting Existing Classes During Class Discovery**
  - Fixed issue where using Discovery to discover a new document type would delete all existing classes from the configuration
  - **Root Cause**: Custom config `classes` array was replacing Default `classes` array during runtime merge, causing loss of existing classes
  - **Solution**: Discovery now reads both Default and Custom classes, merges them with the newly discovered class, and saves the complete merged list to Custom config
  - Ensures discovered classes are additive to existing configuration rather than replacing it

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.4.14.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.4.14.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.4.14.yaml`

## [0.4.13]

### Added

- **Rule Validation for Automated Compliance Assessment**
  - Added Rule Validation module enabling automated validation of extracted document data against configurable business rules and compliance criteria
  - **Key Capabilities**: Validate documents against any domain-specific compliance requirements (healthcare, financial, legal, insurance, manufacturing), customizable pass/fail criteria adaptable to industry needs, concurrent processing with intelligent chunking for large documents
  - **Dual Output Formats**: JSON for programmatic integration and Markdown for human review
  - **Integration**: Integrated into Pattern-2 workflow with AWS Step Functions parallel processing
  - **Example Use Cases**: Healthcare prior authorization validation, loan application compliance checking, contract clause verification, claims validation, quality control
  - **Configuration**: Fully configurable via `rule_validation` settings including custom recommendation options, model selection, and processing limits
  - **Documentation**: Complete guide in `docs/rule-validation.md` 

- **Visual Document Editor Enhancements**
  - **Improved Navigation Controls**: Mouse wheel zoom (no modifier key required) and click-and-drag panning for intuitive document image exploration
  - **Inline Field Editing with S3 Save**: Edit prediction values directly in the visual editor with change tracking, edit history, and direct S3 persistence
  - **Evaluation Baseline Editing**: Edit baseline (expected) values directly in the editor when evaluation data is available, with dedicated save/discard controls and independent change tracking from predictions
  - **Save & Reprocess Workflow**: After saving edits to predictions or baselines, trigger reprocessing to re-run summarization and evaluation with updated data; document automatically transitions through SUMMARIZING → EVALUATING → COMPLETE statuses
  - **Tabbed Interface**: New tabs for Visual Editor (form-based), JSON Editor (raw JSON with section filtering), and Revision History (audit trail with timestamps and field-level diffs)
  - **Smart Filtering**: Filter to show only low-confidence fields or evaluation mismatches; collapsible tree navigation with Expand/Collapse All controls
  - **Evaluation Comparison Mode**: Side-by-side predicted vs expected values with match indicators (✓/⚠), evaluation scores, and LLM-generated comparison reasons
  - **Section Navigation**: Previous/Next buttons to navigate between document sections without closing the editor

- **Section-Level DynamoDB Updates for Parallel Processing Optimization**
  - Added lightweight `updateDocumentStatus` mutation for status-only updates (~500 bytes vs ~100KB full document)
  - Added atomic `updateDocumentSection` mutation for individual section updates using `SET Sections[index] = :value`
  - **Scalability**: Eliminates DynamoDB throttling for very large documents by avoiding full-document read-modify-write cycles
  - **Real-time Updates**: Both new mutations now trigger `onUpdateDocument` subscription for UI synchronization
  - **Pattern-2/3 Integration**: Extraction and assessment functions now use section-level updates instead of full document rewrites

### Fixed

- **Visual Editor Confidence Alerts Filter Not Showing Null Fields** - Fixed issue where the "Confidence Alerts Only" filter in the Document Details visual editor was not displaying fields with `null` values, even when they had low confidence scores in `explainability_info`. The filter now properly detects and shows all low-confidence fields regardless of their value type.

- **Evaluation Failure for Schemas with Empty Nested Objects** - Fixed evaluation failing with "field_definitions must contain at least one field" error when document schemas contain nested objects with empty properties (e.g., `AccidentInformation: {type: object, properties: {}}`). Empty object properties are now automatically filtered during schema processing.

- **Evaluation Report Section Ordering** - Fixed document sections in evaluation markdown reports iterating in alphabetical order (1, 10, 11, 2, 3) instead of numerical order (1, 2, 3, 10, 11) by implementing natural sorting for section IDs

- **Confidence Alerts Mismatch for JSON Schema `$ref` Properties**
  - Fixed issue where confidence alerts in UI showed incorrect counts (all with confidence=0) that didn't match the actual extraction confidence scores in explainability_info JSON
  - **Root Cause**: Properties using JSON Schema `$ref` references were being incorrectly classified as "simple" types instead of "group" (object) types, causing false positive alerts

- **Configuration Import Float Type Error for DynamoDB**
  - Fixed "Float types are not supported. Use Decimal types instead" error when importing configuration files via CLI (`idp-cli config-upload`) or Web UI

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.4.13.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.4.13.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.4.13.yaml`

## [0.4.12]

### Added

- **IDP SDK - Python SDK for Programmatic Document Processing**
  - New `idp_sdk` Python package (`lib/idp_sdk/`) providing a native Python interface for IDP operations
  - **IDPClient Class**: Wraps `idp-cli` commands with Pythonic methods for seamless integration into Python applications
  - **Key Methods**: `run_inference()`, `rerun_inference()`, `download_results()`, `status()`, `deploy()`, `delete()`, `delete_documents()`, `validate_manifest()`, `generate_manifest()`, `config_create()`, `config_validate()`, `config_download()`, `config_upload()`
  - **Pydantic Response Models**: Type-safe response objects (`BatchResult`, `ManifestResult`, `ValidationResult`, `ConfigCreateResult`, `ConfigValidationResult`) with proper Pydantic v2 compatibility
  - **Lambda Integration Example**: Complete SAM template and handler demonstrating SDK usage in AWS Lambda functions
  - **Documentation**: SDK reference guide (`docs/idp-sdk.md`) with CLI command mapping, usage examples, and Lambda patterns
  - **Easy Installation**: `pip install -e lib/idp_sdk` or `make setup` installs SDK with all dependencies
  - **Use Cases**: CI/CD pipelines, Lambda functions, automated workflows, custom integrations, and programmatic batch processing

- **Relocated idp-cli to lib/idp_cli_pkg/**
  - Moved `idp_cli/` directory to `lib/idp_cli_pkg/` to co-locate with other library packages
  - Updated all documentation and Makefile targets for new location

- **Modular System Defaults Architecture for Simplified Configuration**
  - Introduced pattern-specific system default files (`lib/idp_common_pkg/idp_common/config/system_defaults/pattern-{1,2,3}.yaml`) that provide default settings for OCR, classification, extraction, assessment, evaluation, summarization, discovery, and agents
  - User configurations now only need to specify `notes`, `classes`, and any intentional overrides - all other settings inherit from system defaults
  - Simplified all config_library configurations to minimal footprint (most now just 10-30 lines instead of hundreds)
  - Updated all README files in config_library and docs/configuration.md with inheritance documentation
  - **Benefits**: Simpler configs, automatic maintenance when defaults evolve, clearer visibility into customizations

- **Increased Extraction max_tokens Default** - Increased default `max_tokens` for extraction from 10,000 to 65,535 (Nova 2 Lite model maximum) to reduce LLM output truncation on long documents

- **IDP CLI Configuration Management Commands** - [GitHub Issue #87](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/87)
  - `idp-cli config-create` - Generate IDP configuration template from system defaults with selectable feature sets
  - `idp-cli config-validate` - Validate configuration file against system defaults and JSON schema
  - `idp-cli config-download` - Download current configuration from a deployed stack
  - `idp-cli config-upload` - Upload a local configuration file to a deployed stack's DynamoDB ConfigurationTable

- **IDP CLI Auto-Monitor for In-Progress Stack Operations**
  - Enhanced `idp-cli deploy` and `idp-cli delete` commands to automatically detect in-progress CloudFormation operations
  - **Smart Detection**: When running deploy/delete on a stack that's already creating, updating, deleting, or rolling back, the CLI automatically switches to monitoring mode instead of failing
  - **Seamless UX**: If you forget to use `--wait` on the first run, simply run the same command again to monitor progress
  - **Interactive Cancel for Delete**: When running `idp-cli delete` on a stack with CREATE or UPDATE in progress, offers option to cancel the current operation and proceed with deletion

- **New Make Targets and Documentation**
  - Added `make setup` target to install `idp-cli` and `idp_common` packages in development mode
  - Added `make ui-start` target to start UI dev server with optional `STACK_NAME` parameter for auto-generating `.env` from stack outputs
  - Documented all make targets in CONTRIBUTING.md including setup, lint, test, ui-start, commit, and DSR security scanning

- **IDP CLI New Commands for Operations and Testing**
  - Added `idp-cli load-test` command for throughput testing with configurable document rates (1-10,000/min) and dynamic schedule support via CSV files
  - Added `idp-cli stop-workflows` command for batch workflow termination with interactive confirmation and dry-run mode
  - Added `idp-cli delete-documents` command for removing documents and all associated data from the IDP system
  - Added `idp-cli remove-deleted-stack-resources` command for discovering and removing orphaned resources (CloudFront distributions, response header policies, CloudWatch log groups, AppSync APIs, IAM policies, S3 buckets, DynamoDB tables) left behind after IDP stacks are deleted, with multi-region stack discovery, interactive confirmation with "yes/skip all of type" options, and configurable `--check-stack-regions` option
  - Comprehensive unit tests added for all new CLI modules

### Changed

- **Scripts Directory Reorganization**
  - Consolidated development environment setup scripts into `scripts/setup/` subdirectory
  - Moved CI/CD scripts (`codebuild_deployment.py`, `integration_test_deployment.py`, `validate_buildspec.py`, `typecheck_pr_changes.py`, `validate_service_role_permissions.py`) into `scripts/sdlc/` subdirectory
  - Updated all references in `.gitlab-ci.yml`, `Makefile`, and documentation

### Fixed

- **Fixed Document Details Page Not Loading After Browser Refresh for Document IDs Containing Forward Slashes**
  - Fixed issue where navigating to a document with a `/` in the Document ID (e.g., `folder/filename.pdf`) and then refreshing the browser would result in a blank page
  - **Root Cause**: When the browser refreshes a URL containing `%2F` (encoded slash), it automatically decodes it to `/`. React Router's `:objectKey` parameter only captures a single path segment, so `folder/filename.pdf` was being split into multiple segments, causing a route mismatch
  - **Solution**: Changed the route from `path=":objectKey"` to `path="*"` (wildcard route) to capture the full remaining path including any embedded slashes, and updated `DocumentDetails` component to extract the document key from `params['*']`

- **Improved UX for Document List and Document Details Action Buttons**
  - Added hover tooltips to all Document List toolbar buttons (Refresh, Download, Release Review, Abort, Reprocess, Delete) for better discoverability
  - Converted Abort, Reprocess, Delete, and Release Review buttons to icon-only display for a cleaner, more compact toolbar
  - Added `unlocked` icon to Release Review button to visually represent releasing a human review lock

- **Fixed Evaluation Failure for Documents with Truncated LLM Extraction Output**
  - Fixed evaluation service crash when extraction output contained unparsed `raw_output` instead of structured fields
  - **Root Cause**: When LLM extraction output is truncated (model hits max_tokens limit), the extraction service stores `{"raw_output": "..."}` which caused Pydantic validation errors during evaluation
  - **Solution**: 
    - Added `repair_truncated_json()` utility function that attempts to repair truncated JSON using multiple strategies (closing brackets, finding last complete element, extracting complete fields)
    - Integrated JSON repair into extraction service - most truncated output is now automatically repaired
    - Added detection of `raw_output` case in evaluation service with clear error messaging: "Extraction parsing failed... LLM output could not be parsed as valid JSON. This typically indicates truncated output (model hit max_tokens limit). Consider increasing max_tokens in extraction config."
    - Added metadata fields (`output_truncated`, `output_repaired`, `repair_method`) to track truncation/repair status
    - Enhanced evaluation service type coercion to provide appropriate defaults for required fields when LLM returns null values (prevents Pydantic validation errors like "Field required")

- **Fixed AgentRequestHandler Missing Lambda Invoke Permission for Error Analyzer Agent**
  - Fixed AccessDeniedException when clicking the Troubleshoot button in the Web UI

- **Fixed sectionSplitting=disabled Incorrectly Classifying Documents Based on Blank Pages - [GitHub Issue #167](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/167)**
  - Fixed bug where documents with blank pages could be incorrectly classified as `"unclassifiable_blank_page"` when using `sectionSplitting: disabled`
  - **Root Cause**: Page classification results arrive in completion order (not page order) from ThreadPoolExecutor, so blank/simple pages that finish processing first would end up at index 0 and incorrectly determine the document classification
  - **Solution**: Implemented majority voting strategy that:
    - Uses config-defined classes to determine voting eligibility (only pages matching valid document types from configuration can vote)
    - Automatically excludes any classification not in the config (blank pages, errors, LLM hallucinations)
    - Uses majority voting - most common valid classification wins
    - Uses first page's classification as tie-breaker for determinism
    - Falls back to first page's classification when all pages are unclassifiable
  - **Benefits**: Config-driven approach automatically adapts to any defined document classes without hardcoding exclusion lists
  - Updated documentation in `docs/classification.md` explaining the voting behavior

- **Test Results Config Export Not Properly Merging or Formatting**
  - Fixed issue where config export was downloading raw JSON with separate Default/Custom entries instead of merged config

### Removed

- **Obsolete Scripts Migrated to IDP CLI**
  - Removed `simulate_load.py` and `simulate_dynamic_load.py` (replaced by `idp-cli load-test`)
  - Removed `stop_workflows.sh` (replaced by `idp-cli stop-workflows`)
  - Removed `cleanup_orphaned_resources.py` (replaced by `idp-cli remove-deleted-stack-resources`)
  - Removed `lookup_file_status.sh` (replaced by `idp-cli status --document-id`)
  - Removed unused utilities: `add_lambda_layers.py`, `test_layer_build.py`, `test_pip_extras.py`, `compare_json_files.py`, `benchmark_utils/`

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.4.12.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.4.12.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.4.12.yaml`

## [0.4.11]

### Added

- **Built-in Human-in-the-Loop (HITL) Review System**
  - Replaced Amazon SageMaker A2I (Augmented AI) with a built-in HITL review system integrated directly into the Web UI
  - **Persona-Based Access Control**: 
    - **Admin**: Full access to all documents, can skip reviews, release review locks, and manage users
    - **Reviewer**: Access limited to documents pending HITL review, can claim and complete section reviews
  - **Review Workflow Features**:
    - Start Review button to claim document ownership and prevent concurrent edits
    - Section-level review with inline JSON editing and visual document viewer
    - Mark Section Review Complete to approve individual sections
    - Skip All Reviews (Admin only) to bypass pending reviews and continue workflow
    - Release Review to unlock document for other reviewers
  - **Real-time Status Updates**: Review Status, Review Status, Review Owner, and Reviewed By fields update in real-time across all user sessions via GraphQL subscriptions
  - See [Human-in-the-Loop Review Documentation](./docs/human-review.md) for detailed workflow information
  - **Note**: These are Phase 1 of HITL process updates. In upcoming phases, we are working to deliver futher improvements to human review capabilities with the ability to update document classification, extraction, and resubmit for incremental processing as part of a holistic approach to huiman reviews.
- **User Management**
  - New User Management page for Admin users to create and manage additional Admin & Reviewer accounts
  - Cognito user groups (Admin, Reviewer) for role-based access control
  - Automatic user synchronization with Cognito

- **DocSplit-Poly-Seq Test Set Auto-Deployment**
  - Automatically deploys 500 multi-page packet PDFs from HuggingFace dataset (https://huggingface.co/datasets/jordyvl/rvl_cdip_n_mp) during stack deployment
  - **13 Document Types**: invoice, email, form, letter, memo, resume, budget, news article, scientific publication, specification, questionnaire, handwritten, and language (non-English) documents
  - **Multi-Document Packets**: Each of 500 packets contains 2-10 distinct subdocuments of different types for comprehensive splitting and classification testing
  - **Packet Statistics**: 7,330 total pages across 2,027 document sections with average of 14.7 pages and 4.1 sections per packet
  - **Ground Truth Included**: Page-level classification and document boundary information for each packet. Extraction ground truth is not included.
  - **Evaluation Capabilities**: Enables testing of page-level classification accuracy, document splitting accuracy, and split order preservation. Does NOT enable testing of extraction accuracy since there is no extraction ground truth for this data set
  - Test set available in Test Studio UI alongside RealKIE-FCC-Verified and OmniAI-OCR-Benchmark datasets
  - Corresponding configs available in Configuration Library
  - Ideal for evaluating document splitting and classification accuracy in complex multi-document scenarios

### Changed

- **HITL Configuration**
  - HITL is now disabled by default in the configuration
  - Users must explicitly enable HITL in the Configuration page (Assessment & HITL Configuration section) to trigger human review workflows
  - `hitl_enabled` setting controls whether documents with low confidence trigger HITL review

### Removed

- **Amazon SageMaker A2I Resources**
  - Removed SageMaker A2I Flow Definition, Human Task UI, and Workteam resources
  - Removed A2I-related Lambda functions (`create_a2i_resources`, `get-workforce-url`)
  - Removed `EnableHITL` and `PrivateWorkteamArn` CloudFormation parameters

### Changed

- **Lambda Layers Architecture for Improved Build Efficiency**
  - Replaced bundled `idp_common` package dependencies in individual Lambda functions with three shared Lambda Layers
  - **Three Specialized Layers**:
    - `base` layer: Core functionality with docs_service and image extras
    - `reporting` layer: Reporting and analytics dependencies
    - `agents` layer: Agent-related dependencies
  - **Key Benefits**:
    - Reduced SAM build times by eliminating redundant dependency installation across 50+ Lambda functions
    - Layer content-based hashing ensures layers are only rebuilt when actual contents change
    - Automatic removal of Lambda runtime packages (boto3, botocore, etc.) reduces layer sizes by ~100MB
    - Layer zips cached locally and in S3, skipping uploads when content hasn't changed
  - **Build System Integration**: publish.py automatically builds, hashes, and uploads layers before SAM builds

- **Enhanced publish.py Performance and Logging**
  - **Consistent Logging Helpers**: Added 8 standardized logging methods (`log_phase`, `log_task`, `log_detail`, `log_success`, `log_cached`, `log_warning`, `log_error`) for uniform output formatting with colored icons and thread prefixes
  - **Timed S3 Uploads**: Added `upload_to_s3_with_timer()` helper with spinner animation, elapsed time display, and optimized `TransferConfig` for multi-threaded multipart uploads
  - **AWS CLI Config Library Sync**: Replaced boto3 ThreadPoolExecutor-based config library upload (~60 lines) with `aws s3 sync` command for built-in concurrency, delta sync (skip unchanged files), and simpler code
  - **Timing Breakdown Summary**: End-of-build summary shows top 4 time-consuming steps and percentages for build optimization insights
  - **Phase Headers**: Major build phases now display with clear `═══` separator lines and emojis for visual clarity

- **AppSync Resolvers Extracted to Nested Stack for Improved Template Modularity**
  - Refactored main CloudFormation template by extracting 130 AppSync resources into new nested stack architecture
  - **Extracted Components**:
    - Created `nested/appsync/template.yaml` containing GraphQLSchema, AppSyncServiceRole, Lambda resolver functions, LogGroups, DataSources, and Resolvers
    - Moved related Lambda functions from `src/lambda/` to `nested/appsync/src/lambda/` with colocated template definitions
    - Relocated GraphQL schema from `src/api/` to `nested/appsync/src/api/`
  - **Main Template Optimization**: Reduced resource count by keeping only core infrastructure (GraphQLApi, GraphQLApiLogGroup, AppSyncCwlRole, WAF resources, background worker functions)
  - **Build System Integration**: Updated `publish.py` to build nested stack in parallel with patterns
  - **Impact**: Main template now more manageable and faster to navigate, nested stack enables modular development of AppSync resources, parallel builds reduce overall build time

- **Consolidated Nested Stack Directory Structure**
  - Moved `options/bda-lending-project` and `options/bedrockkb` into `nested/` directory for simplified project organization
  - All CloudFormation nested stacks now located in single `nested/` directory alongside `appsync`, `bda-lending-project`, and `bedrockkb`
  - Updated build system to build only two categories concurrently (nested + patterns) instead of three (nested + patterns + options)
  - **Breaking Change**: Directory paths changed - `options/` → `nested/`. Existing work-in-progress branches will have merge conflicts in directory structure.

### Fixed

- **Fixed page_indices Reset Bug in Multi-Section Documents**
  - Fixed issue where all sections in document packets had page_indices starting from 0 instead of their actual position in the original document by pre-calculating indices during classification with access to global minimum page ID and storing in section.attributes for extraction step to use

- **Metering Table Added Requests**
  - Added requests count to bedrock metering data to track API request metrics
  
- **IDP CLI Stack Parameter Preservation During Updates**
  - Fixed bug where `idp-cli deploy` command was resetting ALL stack parameters to their default values during updates, even when users only intended to change specific parameters

### Upgrade Notes

- **⚠️ IMPORTANT: Upgrading from v0.4.11 or earlier**
  - **Complete all pending HITL workflows before upgrading**: Any documents waiting in SageMaker A2I human review loops will be orphaned as A2I resources are deleted during the upgrade
  - **Re-enable HITL after upgrade**: If you previously had `EnableHITL=true` CloudFormation parameter, you must now enable HITL through the Configuration page in the Web UI (Assessment & HITL Configuration → Enable HITL)
  - **User migration**: Existing Cognito users will need to be assigned to Admin or Reviewer groups for HITL access 

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.4.11.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.4.11.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.4.11.yaml`

## [0.4.10]

### Added

- **Enhanced Evaluation Reports with Granular Field Comparison Details (sticker-eval v0.1.4)**
  - Integrated sticker-eval v0.1.4's fine-grain field comparison feature providing detailed nested object match information alongside aggregate scores
  - **Nested Field Details**: For complex attributes (objects, arrays), reports now show individual field-by-field comparisons in addition to aggregate rollup scores
  - **Interactive Report Controls**: 
    - 🔍 "Show Only Unmatched" button to filter and display only problematic fields for focused debugging
    - ➕➖ Expand/Collapse All buttons to control nested detail visibility across the entire report
    - Expandable `<details>` sections for each attribute with nested comparisons
  - **Visual Enhancements**: Aggregate scores clearly marked with blue styling and "(aggregate)" annotation, color-coded rows (green for matched, red for unmatched), HTML tables with field paths and comparison results
  - **JSON Report Structure**: Full `field_comparison_details` array preserved in JSON output for programmatic analysis and consumption by analytics tools
  - **Benefits**: Quickly identify which specific nested fields cause aggregate score drops, compact problem view focusing on unmatched rows, complete diagnostic context with both high-level and granular perspectives

- **BDA / IDP Sync Feature for Pattern-1 Blueprint Synchronization**
  - Added bidirectional synchronization between BDA (Bedrock Data Automation) blueprints and IDP custom document classes
  - **Key Capabilities**: Automatic blueprint creation from IDP classes, automatic IDP class creation from BDA blueprints, intelligent change detection using DeepDiff, automatic cleanup of orphaned blueprints
  - **Sync Process**: Discovery configurations automatically trigger blueprint updates in BDA projects via `sync_bda_idp_resolver` Lambda function
  - **Schema Transformation**: Converts between IDP JSON Schema (draft 2020-12) and BDA blueprint format (draft-07) while preserving semantic meaning
  - **Important Limitations**: AWS managed blueprints excluded from sync, nested objects within objects not supported by BDA, nested arrays within object definitions not supported
  - **Best Practices**: Use flattened schema structures, place arrays only at top-level, validate schema structure before sync, monitor sync results for partial failures
  - **Use Cases**: Maintain consistency between IDP configuration and BDA blueprints, automatically propagate configuration changes, streamline document class management across both systems

- **Separate Pricing Configuration and Management UI**
  - Pricing configuration separated from general IDP configuration into dedicated system
  - New `config_library/pricing.yaml` file with centralized pricing for all AWS services (Textract, Bedrock, BDA, Lambda, SageMaker)
  - New "Pricing" page in Web UI for managing service pricing with:
    - Edit pricing for individual APIs and units (e.g., `bedrock/us.amazon.nova-lite-v1:0` → `inputTokens`, `outputTokens`)
    - Import/Export pricing configurations (JSON/YAML)
  - Used for cost estimation and reporting across all document processing workflows

- **Enhanced Document Pages Editor for Pattern-2 and Pattern-3**
  - Replaced confusing "View/Edit Data" button with intuitive "View Page Text" and "Edit Pages" workflow mirroring the Document Sections panel pattern
  - New modal editor with split-pane layout displaying plain text (left) and live markdown preview (right) - no more raw JSON visible to users
  - Added ability to reset page classifications to force reclassification and edit page text content with immediate S3 saves to prevent data loss
  - Implemented "Save & Process Changes" workflow for selective reprocessing - class resets trigger section removal and reclassification, text modifications trigger re-extraction while preserving sections
  - Resolves #164 

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.4.10.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.4.10.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.4.10.yaml`

## [0.4.9]

### Added

- **OmniAI OCR Benchmark Dataset Auto-Deployment for Test Studio**
  - Automatically deploys 293 document images from OmniAI OCR Benchmark HuggingFace dataset (https://huggingface.co/datasets/getomni-ai/ocr-benchmark) during stack deployment
  - **9 Document Formats**: BANK_CHECK (52), COMMERCIAL_LEASE_AGREEMENT (52), CREDIT_CARD_STATEMENT (11), DELIVERY_NOTE (8), EQUIPMENT_INSPECTION (11), GLOSSARY (31), PETITION_FORM (51), REAL_ESTATE (59), SHIFT_SCHEDULE (18)
  - Pre-selected images filtered for formats with >5 samples per schema for quality benchmarking
  - Complex nested JSON schemas with objects and arrays matching original HuggingFace dataset structure
  - Test set available in Test Studio UI alongside existing RealKIE-FCC-Verified dataset
  - Corresponding config: `config_library/pattern-2/ocr-benchmark/config.yaml` with all 9 document classes
  - Ideal for testing classification across diverse document types and extraction on complex nested schemas
  
- **GovCloud Configuration Library for Pattern-1 and Pattern-2** - [GitHub Issue #162](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/162)
  - Added `lending-package-sample-govcloud` configurations for both Pattern-1 and Pattern-2 with GovCloud-compatible model IDs
  - **Model ID Mappings for GovCloud**:
    - `us.amazon.nova-pro-v1:0` → `amazon.nova-pro-v1:0`
    - `us.amazon.nova-lite-v1:0` → `amazon.nova-lite-v1:0`
    - All other models (Claude, Nova Premier) → `anthropic.claude-3-7-sonnet-20250219-v1:0`
  - Enhanced `generate_govcloud_template.py` to automatically set GovCloud configurations as default when generating GovCloud templates
  - **Automatic Integration**: GovCloud templates now default to `lending-package-sample-govcloud` configuration ensuring proper model IDs without manual configuration

- **Abort Workflow Feature for Stopping In-Progress Document Processing**
  - Added ability to abort document processing workflows directly from the Web UI
  - New "Abort" button available for documents with in-progress status, with confirmation modal to prevent accidental aborts
  - GraphQL mutation `abortWorkflow` enables programmatic workflow cancellation
  - Documents aborted mid-processing are marked with ABORTED status for clear tracking and reporting

- **Global Cross-Region Inference Profile Model Support**
  - Added support for Bedrock global inference profile models enabling cross-region model access
  - **Supported Global Models**:
    - Amazon Nova 2 Lite (`global.amazon.nova-2-lite-v1:0`)
    - Claude Haiku 4.5 (`global.anthropic.claude-haiku-4-5-20251001-v1:0`)
    - Claude Sonnet 4.5 (`global.anthropic.claude-sonnet-4-5-20250929-v1:0`)
    - Claude Sonnet 4.5 - Long Context (`global.anthropic.claude-sonnet-4-5-20250929-v1:0:1m`)
    - Claude Opus 4.5 (`global.anthropic.claude-opus-4-5-20251101-v1:0`)
  - All global models support prompt caching functionality
  - Enables seamless cross-region model invocation without specifying regional endpoints

- **Amazon Bedrock Service Tier Support for Cost and Performance Optimization**
  - Added support for Amazon Bedrock service tiers through model ID suffixes enabling performance and cost optimization
  - **Three Service Tiers Available**:
    - **Priority**: Fastest response times (~25% better latency) with premium pricing - ideal for customer-facing workflows
    - **Standard**: Consistent performance at regular pricing - default choice for most workloads
    - **Flex**: Variable latency with discounted pricing - optimized for batch processing and non-urgent tasks
  - **Model ID Suffix Format**: Append `:flex` or `:priority` to model IDs (e.g., `us.amazon.nova-2-lite-v1:0:flex`)
  - **Supported Models**: Nova 2 Lite models available with all three tier options across US, EU, and Global regions

### Changed

- **Test Studio UI Enhancements for Improved Table Layouts and User Experience**
  - Added resizable columns and CollectionPreferences with wrap lines for all tables in TestComparison and TestResults
  - Combined accuracy and split classification metrics into collapsible "Average Accuracy and Split Metrics" section with expandable "Additional Metrics" for comprehensive review
  - Added color-coded cost comparisons with visual indicators for improved readability

- **Updated Sample Configurations to Use Amazon Nova 2 Lite as Default Model, and remove Textract TABLES, SIGNATURE features**
  - Changed default model to `us.amazon.nova-2-lite-v1:0` for classification, extraction, summarization, and evaluation across all sample configurations in the configuration library
  - Remove Textract TABLES and SIGNATURES options from default config
  - Provides improved cost-efficiency while maintaining strong performance for document processing workflows

- **Improved Publish Script User Experience**
  - Added spinner progress indicators for SAM build and SAM package operations showing real-time elapsed time
  - Added timing metrics summary showing build/package/total duration for main template builds
  - Output now provides visual feedback during long-running operations instead of appearing silent
  - Enabled parallel SAM builds (`sam build --parallel`) for significantly faster build times (~73s vs 4+ minutes)
  - Pre-built wheel approach for idp_common package eliminates race conditions during parallel Lambda builds

- **RealKIE-FCC-Verified Dataset Schema Alignment with HuggingFace**
  - Updated `config_library/pattern-2/realkie-fcc-verified/config.yaml` to match the HuggingFace json_schema exactly
  - Changed `LineItemDays` from array type with enum values to simple string type (matching raw HuggingFace data format)
  - Updated field descriptions to match HuggingFace schema (e.g., "The agency the invoice is addressed to")

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.4.9.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.4.9.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.4.9.yaml`

## [0.4.8]

### Added

- **Section Data Download Feature for Document Results Export**
  - Added compact "Download" dropdown button in Document Sections panel for exporting section processing results
  - **Two Download Options**: 
    - "Download Data" - Downloads prediction results from OutputBucket (always available)
    - "Download Baseline" - Downloads baseline/ground truth data from EvaluationBaselineBucket (only shown when baseline exists)

- **Configuration Library Import Feature for Enhanced Configuration Management**
  - Added Configuration Library browser enabling users to import pre-configured document processing workflows directly from the solution's configuration library
  - **Dual Import Options**: Users can now choose between importing from local files (existing) or from the Configuration Library (new)
  - **Pattern-Aware Filtering**: Automatically displays only configurations compatible with the currently deployed pattern (Pattern 1, 2, or 3)
  - **README Preview**: When available, displays markdown-formatted README documentation before importing to help users understand configuration purpose and features

- **Test Studio Interactive Charts and Document Analysis Enhancements**
  - **Interactive Score Distribution Charts**: Replaced CloudScape chart with native Recharts implementation featuring dual chart support (Bar Chart and Line Chart options with dropdown selector), native interactivity with built-in click events that open document details modal, and optimized layout with improved margins, labels, and space utilization
  - **Lowest Scoring Documents Analysis**: Enhanced TestResults with table showing documents with lowest weighted overall scores, TestComparison with cross-test comparison of problematic documents, user-configurable count dropdown (5, 10, 20, or 50 documents), side-by-side T1 vs T2 comparison format for easy analysis, and clickable document links for direct navigation to document viewer
  - **UI/UX Improvements**: Compact table styling with reduced spacing and improved readability, left-aligned content for better text alignment of document IDs, consistent design matching existing CloudScape design system, and responsive layout where charts adapt to container width

- **RealKIE-FCC-Verified Dataset Auto-Deployment for Test Studio**
  - Automatically deploys 75 FCC invoice documents from HuggingFace public dataset during stack deployment - zero manual steps required
  - Test set immediately available in Test Studio UI with complete ground truth for benchmarking extraction accuracy
  - Version controlled via CloudFormation property - skips re-download on stack updates unless version changes

### Fixed

- **Bedrock OCR Image Resizing Regression - Partial Dimension Configuration Support**
  - Fixed critical regression where configuring only `target_width` (without `target_height`) disabled all image resizing, causing Bedrock OCR to fail with "length limit exceeded" errors
  - **Root Cause**: OCR service used `and` condition requiring both dimensions, rejecting partial configs and sending full-resolution images that exceeded model input limits
  - **Solution**: Implemented aspect-ratio-preserving single-dimension resizing that calculates missing dimension from actual image aspect ratio

- **Test Studio Bug Fixes**
  - Fixed TestSets manual upload issues

- **Agentic Extraction Prompt Caching** - [GitHub PR #156](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/pull/156)
  - Removed additional cachepoints to prevent prompt caching conflicts in agentic extraction

- **GovCloud S3 Vectors Service Principal Deployment Failure** - [GitHub Issue #159](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/159)
  - Fixed CloudFormation deployment failure in GovCloud regions caused by S3 Vectors service not being available
  - **Root Cause**: KMS key policy referenced `indexing.s3vectors.${AWS::URLSuffix}` service principal which doesn't exist in GovCloud (us-gov-west-1, us-gov-east-1)

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.4.8.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.4.8.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.4.8.yaml`

## [0.4.7]

### Added

- **MCP Integration Cross-Region Support for QuickSuite Integration**
  - Added cross-region support for QuickSuite integration enabling MCP connectivity across multiple AWS regions: us-east-1, us-west-2, eu-west-1, ap-southeast-2

### Fixed

- **Stack deployment failure due to MCP Integration IAM Permissions - [GitHub Issue #154](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/154)**
  - Fixed missing permissions in AgentCoreGatewayManagerFunctionRole by creating the AgentCoreGateway execution role explicitly in the CloudFormation template instead of dynamically in the Lambda function

- **Post-Processing Lambda Hook Compression Handling - [GitHub Issue #155](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/155)**
  - Added intermediate decompression lambda to handle document decompression before invoking custom post-processing lambdas
  - **Root Cause**: After introducing document compression, the post-processing lambda hook was receiving compressed documents in the EventBridge payload, forcing external lambdas to import `idp_common` package and handle decompression manually
  - **Solution**: New `PostProcessingDecompressor` lambda function intercepts EventBridge events, decompresses documents using `Document.load_document()`, and invokes custom post-processors with decompressed payload
  - **Benefits**: Maintains backward compatibility, eliminates external dependencies (no `idp_common` import needed), keeps compression/decompression logic encapsulated within IDP stack, minimal performance impact (<1s latency)

- **Enhanced Bedrock Error Handling for Agent Companion Chat**
  - Implemented robust error handling system for Bedrock API errors in Agent Companion Chat feature with automatic retry and graceful degradation
  - **Automatic Retry with Exponential Backoff**: Configured boto3 with adaptive retry mode (3 attempts) and exponential back-off to prevent service overload
  - **User-Friendly Error Messages**: Created `BedrockErrorMessageHandler` to convert technical errors into clear, actionable messages for service unavailable (503), throttling (429), access denied (403), validation errors (400), timeouts (408), and quota exceeded scenarios
  - **Sub-Agent Error Handling**: When sub-agents (Analytics, Error Analyzer, Code Intelligence) encounter Bedrock errors, the orchestrator continues gracefully without crashing, only displaying the first error to avoid duplicates while allowing other sub-agents to complete

- **GovCloud Template Generation - Missing AppSync and MCP Resource Removal**
  - Fixed CloudFormation deployment error "Unresolved resource dependencies [DeleteDocumentResolverFunction]" when deploying GovCloud templates
  - **Test Studio Resources Added (36 resources)**: Added all Test Studio Lambda functions, AppSync resolvers, data sources, and supporting infrastructure to removal list (DeleteTestsResolver, TestRunnerResolver, TestResultsResolver, TestSetResolver, and all related functions, queues, and policies)
  - **MCP/AgentCore Gateway Resources Added (7 resources)**: Added MCP integration resources that depend on Cognito UserPool to removal list (AgentCoreAnalyticsLambdaFunction, AgentCoreGatewayManagerFunction, AgentCoreGatewayExecutionRole, AgentCoreGateway, ExternalAppClient)
  - **MCP Outputs Removed (8 outputs)**: Removed MCP-related outputs that reference deleted resources (MCPServerEndpoint, MCPClientId, MCPClientSecret, MCPUserPool, MCPTokenURL, MCPAuthorizationURL, DynamoDBAgentTableName, DynamoDBAgentTableConsoleURL)
  - **EnableMCP Default Changed**: Set `EnableMCP` parameter default to 'false' for GovCloud since MCP integration requires Cognito authentication infrastructure
  - **Impact**: GovCloud templates now deploy successfully without dependency errors, maintaining core document processing functionality in headless mode

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.4.7.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.4.7.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.4.7.yaml`

## [0.4.6]

### Added

- **New State-Of-The-Art LLM Model Support**
  - Added support for Amazon Nova 2 Lite model (`us.amazon.nova-2-lite-v1:0`, `eu.amazon.nova-2-lite-v1:0`)
  - Added support for Claude Opus 4.5 model (`us.anthropic.claude-opus-4-5-20251101-v1:0`, `eu.anthropic.claude-opus-4-5-20251101-v1:0`)
  - Added support for Qwen 3 VL model (`qwen.qwen3-vl-235b-a22b`)
  - Available for configuration across all document processing steps

- **Test Studio for Comprehensive Test Management and Analysis**
  - Added unified web interface for managing test sets, running tests, and analyzing results directly from the UI
  - **Test Sets Tab**: Create and manage reusable test collections with three creation methods:
    - Pattern-based creation with file patterns to match existing data sets (Input Bucket and Test Set Bucket)
    - Zip upload with automatic extraction of `input/` and `baseline/` folder structure
  - **Test Executions Tab**: Unified interface combining test execution and results management:
    - Real-time status monitoring
    - Multi-select comparison for side-by-side test analysis
    - Integrated export and delete operations
  - **Key Features**: File structure validation, progress-aware status updates, cached metrics for improved performance, dual bucket support for flexible test organization
  - **Documentation**: Guide in `docs/test-studio.md` with architecture details and workflow examples

- **MCP Integration for External Application Access**
  - Added MCP (Model Context Protocol) integration enabling external applications (like Amazon Quick Suite) to access IDP analytics through AWS Bedrock AgentCore Gateway with secure OAuth 2.0 authentication
  - Implemented Analytics Agent with `search_genaiidp` tool for natural language queries of processed document data (statistics, trends, confidence scores, processing status)
  - Controlled by `EnableMCP` parameter (default: true); provides MCPServerEndpoint and authentication outputs for external application integration; documentation in `docs/mcp-integration.md`

- **Configurable Section Splitting Strategies for Enhanced Document Segmentation Control**
  - Added new `sectionSplitting` configuration option to control how classified pages are grouped into document sections
  - **Three Strategies Available**:
    - `disabled`: Entire document treated as single section with first detected class (simplest case)
    - `page`: One section per page preventing automatic joining of same-type documents (deterministic, solves Issue #146)
    - `llm_determined`: Uses LLM boundary detection with "Start"/"Continue" indicators (default, maintains existing behavior)
  - **Key Benefits**: Deterministic splitting for long documents with multiple same-type forms (e.g., multiple W-2s, multiple invoices), eliminates LLM boundary detection failures for critical government form processing, provides flexibility across simple to complex document scenarios
  - Resolves #146

### Changed

- **Improved Temperature and Top_P Parameter Logic for Deterministic Output**
  - Changed inference parameter selection logic to allow `temperature=0.0` for deterministic output (recommended by Anthropic and other model providers)
  - **New Logic**: Uses `top_p` only when it has a positive value (> 0); otherwise uses `temperature` including `temperature=0.0`
  - **Previous Logic**: Used `top_p` whenever `temperature=0.0`, preventing proper deterministic configuration
  - **Key Benefits**: Enables proper deterministic output with `temperature=0.0`, more intuitive parameter behavior, aligns with model provider best practices (Anthropic recommends `temperature=0` for consistent outputs)
  - **Affected Components**: Bedrock client (`lib/idp_common_pkg/idp_common/bedrock/client.py`), Agentic extraction service (`lib/idp_common_pkg/idp_common/extraction/agentic_idp.py`)
  - **Configuration Guidance**: Set `top_p: 0` to use `temperature` parameter; set `top_p` to positive value to override temperature
  - Set temperature to 0.0 in discovery config for deterministic discovery output (was previously set to 1.0)
  - Set top_p to 0.0 in all repo config files to force use of temperature setting by default.

- **Removed page image limit entirely across all IDP services**
  - removed image limits from multimodal inference steps (classification, extraction, assessment) following Amazon Bedrock API removal of image count restrictions. The system now processes all document pages without artificial truncation, with info logging to track image counts for monitoring purposes.
  - Resolves #147

- **Knowledge Base Vector Store Default Changed to S3 Vectors**
  - Changed default `KnowledgeBaseVectorStore` from `OPENSEARCH_SERVERLESS` to `S3_VECTORS` for cost-optimized deployments
  - S3 Vectors provides 40-60% lower storage costs with sub-second latency suitable for most use cases
  - OpenSearch Serverless remains available for applications requiring sub-millisecond query performance
  - No action required for existing deployments - only affects new stack deployments

### Fixed

- **UI: Document Schema Editor Regex Fields Not Persisting** - Fixed issue where Document Name Regex and Page Content Regex fields were not being saved in configuration or restored after page refresh. Fixes #151
- **Document Schema Builder Enum Support** - Fixed enum value handling in schema builder to properly support enumeration constraints for attribute definitions
- **Agentic Extraction Parameter Passing** - Fixed temperature and top_p parameters now correctly passed to agentic extraction service, enabling proper model behavior control
- **Document Schema Builder UI Labels** - Enhanced field labels and formats in document schema builder for improved clarity and user experience
- **Retry Mechanism Improvements** - Enhanced retry logic for more reliable error handling and recovery across document processing workflows
- **Type Safety Enhancements** - Improved type annotations and fixed undefined items handling to prevent runtime errors

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.4.6.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.4.6.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.4.6.yaml`

## [0.4.5]

### Added

- **Document Split Classification Metrics for Evaluating Page-Level Classification and Document Segmentation**
  - Added `DocSplitClassificationMetrics` class for comprehensive evaluation of document splitting and classification accuracy
  - **Three Accuracy Types**: Page-level classification accuracy, split accuracy without order consideration, and split accuracy with exact page order matching
  - **Visual Reporting**: Generates markdown reports with color-coded indicators (🟢 Excellent, 🟡 Good, 🟠 Fair, 🔴 Poor), progress bars, and detailed section analysis tables
  - **Automatic Integration**: Integrates with evaluation service when ground truth and predicted sections are available
  - **Documentation**: Guide in `lib/idp_common_pkg/idp_common/evaluation/README.md` with usage examples, metric explanations, and best practices

- **Caching improvements to Agentic Extraction Service**
  - Optimized prompt caching by caching document context (text/images) on first LLM call, reducing token costs and quota consumption

- **Enhanced Bedrock Retry Logic for Agentic Extraction**
  - New `bedrock_utils.py` module with exponential backoff and comprehensive error handling
  - Improves agentic extraction reliability for transient failures and rate limiting

- **Review Agent Model Configuration**
  - Added `review_agent_model` parameter to enable separate model for reviewing extraction work
  - Defaults to main extraction model if not specified
  - Configurable through Web UI extraction settings

### Fixed

- **Evaluation Output URI Fields Lost Across All Patterns - causing (a) missing Page Text Confidence content in UI, (2) failed Assessment step when reprocessing document after editing classes (No module named 'fitz')**
  - Fixed bug where `text_confidence_uri` was being set to null in evaluation output for all three patterns
  - Root cause: AppSync service `_appsync_to_document()` method incorrectly mapped page URIs, and evaluation functions overwrote correct documents with corrupted AppSync responses

- **UI: Metering Data Not Displayed During Document Processing**
  - Fixed UI subscription query missing `Metering` field, preventing real-time cost display
  - Users can now see estimated costs accumulate in real-time without manual page refresh

- **UI: Estimated Cost Panel Arrow Misalignment**
  - Fixed expand/contract arrow displaying above "Estimated Cost" heading

- **Agentic Extraction Reliability Improvements**
  - Updated Pydantic model serialization to use `model_dump(mode="json")` for proper JSON handling
  - Resolved linting issues and improved code quality across extraction modules

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.4.5.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.4.5.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.4.5.yaml`

## [0.4.4]

### Added

- **IDP CLI --from-code Flag for Local Development Deployment**
  - Added `--from-code` flag to `idp-cli deploy` command enabling deployment directly from local source code
  - Automatically builds project using `publish.py` script with streaming output for real-time build progress
- **IDP CLI --no-rollback Flag for Stack Deployment Troubleshooting**
  - Added `--no-rollback` flag to `idp-cli deploy` command to disable automatic rollback on CloudFormation stack creation failure
  - When enabled, failed stacks remain in `CREATE_FAILED` state instead of rolling back, allowing inspection of failed resources for troubleshooting

- **Add support for prompt caching for Claude Haiku 4.5**

- **Add support for prompt caching for for EU region models**

### Fixed

- **Analytics Agent Schema Provider - Fixed Nested Attribute Column Display**
  - Fixed `schema_provider.py` to correctly display leaf-level nested columns instead of showing group-level attributes

- **IDP Agent Companion Chat UX improvements**
  - Improved speed of rendering chat response by buffering the agent tool responses.
  - Displaying agent tool queries and results in a modal with formatted results.

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.4.4.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.4.4.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.4.4.yaml`

## [0.4.3]

### Fixed

- Fix #134 - Doc class dropdown shows no options when editing sections
- Fix #133 - Cast topK to int to defend against transient ValidationException exceptions
- Fix #132 - TRACKING_TABLE environment variable needed in EvaluationFunction
- Fix #131 - HITL functions broken post docker migration
- Fix #130 - Enable EU models for Agent Configuration and KB Configuration
- Add ServiceUnavailableException to retryable exceptions in statemachine to better defend against processing failure due to quota overload
- Evaluation Configuration Robustness
  - Improved JSON Schema error messages with actionable diagnostics when configuration issues occur
  - Added automatic type coercion for numeric constraints (e.g., `maxItems: "7"` → `maxItems: 7`) to handle common YAML parsing quirks gracefully
- UI: Document Schema Editor Input Field Fixes
  - Fixed Examples, Default Value, Const, and Enum Values fields not allowing first character deletion or comma input
  - Fixed Enum field remaining disabled after clearing Const value
  - Fixed "Clear all enum values" button not working
  - Fixed empty Evaluation Method picklist for Array[String] and other simple array types

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.4.3.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.4.3.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.4.3.yaml`

## [0.4.2]

### Added

- **Stickler-Based Evaluation System for Enhanced Comparison Capabilities**
  - Migrated evaluation service from custom comparison logic to [AWS Labs Stickler library](https://github.com/awslabs/stickler/tree/main) for structured object evaluation
  - **Field Importance Weights**: New capability to assign business criticality weights to fields (e.g., shipment ID weight=3.0 vs notes weight=0.5)
  - **Enhanced Configuration**: Added `x-aws-idp-evaluation-*` extensions for evaluation configuration
  - **Backward compatible**: Maintained API compatibility - all existing code works unchanged
  - **Enhanced Comparators**: Leverages Stickler's optimized comparison algorithms (Exact, Levenshtein, Numeric, Fuzzy, Semantic) with LLM evaluation preserved through custom wrapper
  - **Better List Matching**: Hungarian algorithm via Stickler for optimal list comparisons regardless of order

- **UI: Evaluation Configuration in Document Schema UI**
  - Added evaluation weight, threshold (with conditional display), and document-level match threshold fields for complete Stickler configuration control
  - Added LEVENSHTEIN and HUNGARIAN evaluation methods with auto-populated threshold defaults based on selected method
  
- **IDP CLI Force Delete All Resources Option**
  - Added `--force-delete-all` flag to `idp-cli delete` command for comprehensive stack cleanup
  - **Post-CloudFormation Cleanup**: Analyzes resources after CloudFormation deletion completes to identify retained resources (DELETE_SKIPPED status)
  - **Use Cases**: Complete test environment cleanup, CI/CD pipelines requiring full teardown, cost optimization by removing all retained resources

### Changed

- **Containerized Pattern-1 and Pattern-3 Deployment Pipelines**
  - Migrated Pattern-1 and Pattern-3 Lambda functions to Docker image deployments (following Pattern-2 approach from v0.3.20)
  - Builds and pushes all Lambda images via CodeBuild with automated ECR cleanup
  - Increases Lambda package size limit from 250 MB (zip) to 10 GB (Docker image) to accommodate larger dependencies

- **Agent Companion Chat - Chat History Feature**
  - Added chat history feature from Agent Analysis back into Agent Companion Chat
  - Users can now load and view previous chat sessions with full conversation context
  - Chat history dropdown displays recent sessions with timestamps and message counts

### Fixed

- **Agent Companion Chat - Session Persistence and input control**
  - Agent Companion Chat in-session memory now persists even when user changes pages
  - Prompt input is disabled during active streaming responses to prevent concurrent requests
  - Fixed issue where charts in loaded chat history were not displaying

- **GovCloud Template Generation errors**
  - Fixed CloudFormation deployment error `Fn::GetAtt references undefined resource GraphQLApi` when deploying GovCloud templates

- **Example Notebook error fixed**
  - Example notebooks updated to work with new v0.4.0+ JSON schema

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.4.2.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.4.2.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.4.2.yaml`

## [0.4.1]

### Changed

- **Configuration Library Updates with JSON Schema Support**
  - Updated configuration library with JSON schema format for lending package, bank statement, and RVL-CDIP package samples
  - Enhanced configuration files to align with JSON Schema Draft 2020-12 format introduced in v0.4.0
  - Updated notebooks and documentation to reflect JSON schema configuration structure

### Fixed

- **UI Few Shot Examples Display** - Fixed issue where few shot examples were not displaying correctly from configuration in the Web UI
- **Re-enabled Regex Functionality** - Restored document name and page content regex functionality for Pattern-2 classification that was temporarily missing
- **Pattern-2 ECR Enhanced Scanning Support** - Added required IAM permissions (inspector2:ListCoverage, inspector2:ListFindings) to Pattern2DockerBuildRole to support AWS accounts with Amazon Inspector Enhanced Scanning enabled. Also added KMS permissions (kms:Decrypt, kms:CreateGrant) for customer-managed encryption keys. This resolves AccessDenied errors and CodeBuild timeouts when deploying Pattern-2 in accounts with enhanced scanning enabled.
- **Reporting Database Data Loss After Evaluation Refactoring - Fixes #121**
  - Fixed bug where metering data and document_section data stopped being written to the reporting database after evaluation was migrated from EventBridge to Step Functions workflow
- **IDP CLI Deploy Command Parameter Preservation Bug**
  - Fixed bug where `idp-cli deploy` command was resetting ALL stack parameters to their default values during updates, even when users only intended to change specific parameters
- **Pattern-2 Deployment Intermittent Lambda (HITLStatusUpdateFunction) ECR Access Failure**
  - Fixed intermittent "Lambda does not have permission to access the ECR image" (403) errors during Pattern-2 deployment
  - **Root Cause**: Race condition where Lambda functions were created before ECR images were fully available and scannable
  - **Solution**: Enhanced CodeBuild custom resource to verify ECR image availability before completing, including:
    - Verification that all required Lambda images exist in ECR repository
    - Check that image scanning is complete (repository has `ScanOnPush: true`)
  - **New Parameter**: Added `EnablePattern2ECRImageScanning` parameter (current default: false) to allow users to enable/disable ECR vulnerability scanning if experiencing deployment issues
    - Recommended: Set enabled (true) for production to maintain security posture
    - Optional: Disable (false) only as temporary workaround for deployment reliability
- **Resolved failing Docker build issue related to Python pymupdf package version update**
  - Pinned pymupdf version to prevent attempted (failing) deployment of newly published version (which is missing ARM64 wheels)

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.4.1.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.4.1.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.4.1.yaml`

## [0.4.0]

> **⚠️ IMPORTANT NOTICE - SIGNIFICANT CONFIGURATION CHANGES**
>
> This release introduces **significant changes to the accelerator configuration** for defining document classes and attributes. The configuration format has been migrated to JSON Schema standards, which provides enhanced flexibility and validation capabilities.
>
> While automatic migration is provided for backward compatibility, **customers MUST fully test this update in a non-production environment** before upgrading production systems. We strongly recommend:
>
> 1. Deploy the update to a test/development environment first
> 2. Verify all document processing workflows function as expected
> 3. Test with representative samples of your production documents
> 4. Review the migration guide at [docs/json-schema-migration.md](./docs/json-schema-migration.md)
> 5. Only proceed with production upgrade after thorough validation
>
> **Do not upgrade production systems without completing validation testing.**

### Added

- **Agent Companion Chat Experience**
  - Added comprehensive interactive AI assistant interface providing real-time conversational support for the IDP Accelerator
  - **Session-Based Architecture**: Transformed from job-based (single request/response) to session-based (multi-turn conversations) with unified agentic chat experience
  - **Persistent Chat Memory**: DynamoDB-backed conversation history with automatic loading of last 20 turns, turn-based message grouping, and intelligent context management with sliding window optimization
  - **Real-Time Streaming**: AppSync GraphQL subscriptions enable incremental response streaming with proper async task cleanup and thinking tag removal for clean display
  - **Code Intelligence Agent**: New specialized agent for code-related assistance with DeepWiki MCP server integration, security guardrails to prevent sensitive data exposure, and user-controlled opt-in toggle (default: enabled)
  - **Rich Chat Interface**: Modern UI with CloudScape Design System featuring real-time message streaming, multi-agent support (Analytics, Code Intelligence, Error Analyzer, General), Markdown rendering with syntax highlighting, structured data visualization (charts via Chart.js, sortable tables), expandable tool usage sections, sample prompts, and auto-scroll behavior
  - **Privacy & Security**: Explicit user consent for Code Intelligence third-party services, session isolation with unique session IDs, error boundary protection, input validation

- **JSON Schema Format for Class Definitions** - [docs/json-schema-migration.md](./docs/json-schema-migration.md)
  - Document class definitions now use industry-standard JSON Schema Draft 2020-12 format for improved flexibility and tooling integration
  - **Standards-Based Validation**: Leverage standard JSON Schema validators and tooling ecosystem for better configuration validation
  - **Enhanced Extensibility**: Custom IDP properties use standard JSON Schema extension pattern (`x-aws-idp-*` prefix) for clean separation of concerns
  - **Modern Data Contract**: Define document structures using widely-adopted JSON Schema format with robust type system (`string`, `number`, `boolean`, `object`, `array`)
  - **Nested Structure Support**: Natural representation of complex documents with nested objects and arrays using JSON Schema's native `properties` and `items` keywords
  - **Automatic Migration**: Existing legacy configurations automatically migrate to JSON Schema format on first load - completely transparent to users
  - **Backward Compatible**: Legacy format remains supported through automatic migration - no manual configuration updates required
  - **Comprehensive Documentation**: New migration guide with format comparison, field mapping table, and best practices

- **IDP CLI Single Document Status Support with Programmatic Output**
  - Enhanced `status` command to support checking individual document status via new `--document-id` option as alternative to `--batch-id`
  - Added programmatic output capabilities with exit codes (0=success, 1=failure, 2=processing) for scripting and automation
  - JSON format output (`--format json`) provides structured data for parsing in CI/CD pipelines and scripts
  - Live monitoring support with `--wait` flag works for both batch and single document status checks
  - Mutual exclusion validation ensures only one of `--batch-id` or `--document-id` is specified
- **Error Analyzer CloudWatch Tool Enhancements**
  - Enhanced CloudWatch log filtering with request ID-based filtering for more targeted error analysis
  - Improved XRay tool tracing and logging capabilities for better diagnostic accuracy
  - Enhanced error context correlation between CloudWatch logs and X-Ray traces
  - Consolidated and renamed tools
  - Provided tools access to agent
  - Updated system prompt

- **Error Analyzer CloudWatch Tool Enhancements**
  - Enhanced CloudWatch log filtering with request ID-based filtering for more targeted error analysis
  - Improved XRay tool tracing and logging capabilities for better diagnostic accuracy
  - Enhanced error context correlation between CloudWatch logs and X-Ray traces
  - Consolidated and renamed tools
  - Provided tools access to agent
  - Updated system prompt

### Fixed

- **UI Robustness for Orphaned List Entries** - [#102](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/102)
  - Fixed UI error banner "failed to get document details - please try again later" appearing when orphaned list entries exist (list# items without corresponding doc# items in DynamoDB tracking table)
  - **Root Cause**: When a document had a list entry but no corresponding document record, the error would trigger UI banner and prevent display of all documents in the same time shard
  - **Solution**: Enhanced error handling to gracefully handle missing documents - now only shows error banner if ALL documents fail to load, not just one
  - **Enhanced Debugging**: Added detailed console logging with full PK/SK information for both list entries and expected document entries to facilitate cleanup of orphaned records
  - **User Impact**: All valid documents now display correctly even when orphaned list entries exist; debugging information available in browser console for identifying problematic entries

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.4.0.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.4.0.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.4.0.yaml`

## [0.3.21]

### Added

- **Claude Sonnet 4.5 Haiku Model Support**
  - Added support for Claude Haiku 4.5
  - Available for configuration across all document processing steps

- **X-Ray Integration for Error Analyzer Agent**
  - Integrated AWS X-Ray tracing tools to enhance diagnostic capabilities of the error analyzer agent
  - X-Ray context enables better distinction between infrastructure issues and application logic failures
  - Added trace ID persistence in DynamoDB alongside document status for complete traceability
  - Enhanced CloudWatch error log filtering for more targeted error analysis
  - Simplified CloudWatch results structure for improved readability and analysis
  - Updated error analyzer recommendations to leverage X-Ray insights for more accurate root cause identification

- **EU Region Support with Automatic Model Mapping**
  - Added support for deploying the solution in EU regions (eu-central-1, eu-west-1, etc.)
  - Automatic model endpoint mapping between US and EU regions for seamless deployment
  - Comprehensive model mapping table covering Amazon Nova and Anthropic Claude models
  - Intelligent fallback mappings when direct EU equivalents are unavailable
  - Quick Launch button for eu-central-1 region in README and deployment documentation
  - IDP CLI now supports eu-central-1 deployment with automatic template URL selection
  - Complete technical documentation in `docs/eu-region-model-support.md` with best practices and troubleshooting

### Changed

- **Migrated Evaluation from EventBridge Trigger to Step Functions Workflow**
  - Moved evaluation processing from external EventBridge-triggered Lambda to integrated Step Functions workflow step
  - **Race Condition Eliminated**: Evaluation now runs inside state machine before WorkflowTracker marks documents COMPLETE, preventing premature completion status when evaluation is still running
  - **Config-Driven Control**: Evaluation now controlled by `evaluation.enabled` configuration setting instead of CloudFormation stack parameter, enabling runtime control without stack redeployment
  - **Enhanced Status Tracking**: Added EVALUATING status to document processing pipeline for better visibility of evaluation progress
  - **UI Improvements**: Added support for displaying EVALUATING status in processing flow viewer and "NOT ENABLED" badge when evaluation is disabled in configuration
  - **Consistent Pattern**: Aligns evaluation with summarization and assessment patterns for unified feature control approach

- **Migrated UI Build System from Create React App to Vite**
  - Upgraded to Vite 7 for faster build times
  - Updated to React 18, AWS Amplify v6, react-router-dom v6, and Cloudscape Design System
  - Reduced dependencies and node_modules size
  - Implemented strategic code splitting for improved performance
  - Environment variables now use `VITE_` prefix instead of `REACT_APP_` for local development

### Fixed

- **IDP CLI Code Cleanup and Portability Improvements** - [#91](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/91), [#92](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/92)
  - Removed dead code from previous refactors in batch_processor.py (51 lines)
  - Replaced hardcoded absolute paths with dynamic path resolution in rerun_processor.py for cross-platform compatibility

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.3.21.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.3.21.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.3.21.yaml`

## [0.3.20]

### Added

- **Agentic extraction preview with Strands agents (experimental)** introducing intelligent, self-correcting document extraction with improved schema compliance and accuracy improvements over traditional methods.
  - Leverages the Strands Agent framework with iterative validation loops and automatic error correction to deliver schema compliance
  - Provides structured output through Pydantic models with built-in validators, automatic retry handling, and superior handling of complex nested structures and date standardization
  - Includes sample notebooks and configuration assets demonstrating agentic extraction for Pattern-2 lending documents
  - Programmatic access available via `structured_output` function in `lib/idp_common_pkg/idp_common/extraction/agentic_idp.py`
  - Currently this is an experimental feature. Future extensibility includes UI-based validation customization, code generation, and Model Context Protocol (MCP) integration for external data enrichment during extraction

- **IDP CLI - Command Line Interface for Batch Document Processing**
  - Added CLI tool (`idp_cli/`) for programmatic batch document processing and stack management
  - **Key Features**: Deploy/update/delete CloudFormation stacks, process and reprocess documents from local directories or S3 URIs, live progress monitoring with rich terminal UI, download processing results locally, validate manifests before processing, generate manifests from directories with automatic baseline matching
  - **Selective Reprocessing**: New `rerun-inference` command to reprocess documents from specific pipeline steps (classification or extraction) while leveraging existing OCR data for cost/time optimization
  - **Evaluation Framework**: Workflow for accuracy testing including initial processing, manual validation, baseline creation, and automated evaluation with detailed metrics
  - **Analytics Integration**: Query aggregated results via Athena SQL or use Agent Analytics in Web UI for visual analysis
  - **Use Cases**: Rapid configuration iteration, large-scale batch processing, CI/CD integration, automated accuracy testing, automated environment cleanup, prompt engineering experiments
  - **Documentation**: README with Quick Start, Commands Reference, Evaluation Workflow, and troubleshooting guides

- **Extraction Results Integration in Summarization Service**
  - Integrates extraction results from the extraction service into summarization module for context-aware summaries
  - **Features**: Fully backward compatible (works with or without extraction results), automatic section handling, error resilient with graceful continuation, comprehensive logging
  - **Configuration**: Enable by adding `{EXTRACTION_RESULTS}` placeholder to `task_prompt` in config.yaml
  - **Benefits**: Context-aware summaries referencing extracted values, improved accuracy and quality, better extraction-summary alignment

### Changed

- **Containerized Pattern-2 deployment pipeline** that builds and pushes all Lambda images via CodeBuild using the new Dockerfile, plus automated ECR cleanup and tests.
  - Lambda docker image deployments have a 10 GB image size limit compared to the 250 MB zip limit of regular deployment. This however doesn't allow for viewing the code in the AWS console.
    The change was introduced to accommodate the increased package size of introducing Strands into the package dependencies.

### Fixed
- **Discovery function times out when processing large documents.**
  - increase lambda discovery processor timeout to 900s
- **Corrected baseline directory structure documentation in evaluation.md**
  - Fixed incorrect baseline structure showing flat `.json` files instead of proper directory hierarchy
  - Updated to correct structure: `<document-name>/sections/1/result.json`
  - Reorganized document for better logical flow and user experience
- **GovCloud Template Generation - Removed GraphQLApi References** - #82
  - Fixed invalid GovCloud template generation where ProcessChanges AppSync resources were not being removed, causing "Fn::GetAtt references undefined resource GraphQLApi" errors
  - Updated `scripts/generate_govcloud_template.py` to remove all ProcessChanges-related resources and extend AppSync parameter cleanup to all pattern stacks
  - Fixed InvalidClientTokenId validation error by ensuring CloudFormation client uses the correct region when validating templates (commercial vs GovCloud)
- **Enhanced Processing Flow Visualization for Disabled Steps**
  - Fixed UX issue where disabled processing steps (when `summarization.enabled: false` or `assessment.enabled: false` in configuration) appeared visually identical to active steps in the "View Processing Flow" display
  - **Key Benefit**: Users can now immediately see which steps are actually processing data vs. steps that execute but skip processing based on configuration settings, preventing confusion about whether summarization or assessment ran
  - Limitation: the new visual indicators are driven from the current config, which may have been altered since the document was processed. We will address this in a later release. See Issue #86.

### Known Issues
- **GovCloud Deployments fail, due to lack of ARM support for CodeBuild. Fix targeted for next release.**

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.3.20.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.3.20.yaml`

## [0.3.19]

### Added

- **Error Analyzer (Troubleshooting Tool) for AI-Powered Failure Diagnosis**
  - Introduced intelligent AI-powered troubleshooting agent that automatically diagnoses document processing failures using Claude Sonnet 4 with the Strands agent framework
  - **Key Capabilities**: Natural language query interface, intelligent routing between document-specific and system-wide analysis, multi-source data correlation (CloudWatch Logs, DynamoDB, Step Functions), root cause identification with actionable recommendations, evidence-based analysis with collapsible log details
  - **Web UI Integration**: Accessible via "Troubleshoot" button on failed documents with real-time job status, progress tracking, automatic job resumption, and formatted results (Root Cause, Recommendations, Evidence sections)
  - **Tool Ecosystem**: 8 specialized tools including analyze_errors (main router), analyze_document_failure, analyze_recent_system_errors, CloudWatch log search tools, DynamoDB integration tools, and Lambda context retrieval - additional tools will be added as the feature evolves.
  - **Configuration**: Configurable via Web UI including model selection (Claude Sonnet 4 recommended), system prompt customization, max_log_events (default: 5), and time_range_hours_default (default: 24)
  - **Documentation**: Comprehensive guide in `docs/error-analyzer.md` with architecture diagrams, usage examples, best practices, troubleshooting guide.

- **Claude Sonnet 4.5 Model Support**
  - Added support for Claude Sonnet 4.5 and Claude Sonnet 4.5 - Long Context models
  - Available for configuration across all document processing steps

### Fixed

- **Problem with setting correctly formatted WAF IPv4 CIDR range** - #73

- **Duplicate Step Functions Executions on Document Reprocess - [GitHub Issue #66](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/66)**
  - Eliminated duplicate workflow executions when reprocessing large documents (>40MB, 500+ pages)
  - **Root Cause**: S3 `copy_object` operations were triggering multiple "Object Created" events for large files, causing `queue_sender` to create duplicate document entries and workflow executions
  - **Solution**: Refactored `reprocess_document_resolver` to directly create fresh Document objects and queue to SQS, completely bypassing S3 event notifications
  - **Benefits**: Eliminates unnecessary S3 copy operations (cost savings)

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.3.19.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.3.19.yaml`

## [0.3.18]

### Added

- **Lambda Function Execution Cost Metering for Complete Cost Visibility**
  - Added Lambda execution cost tracking to all core processing functions across all three processing patterns
  - **Dual Metrics**: Tracks both invocation counts ($0.20 per 1M requests) and GB-seconds duration ($16.67 per 1M GB-seconds) aligned with official AWS Lambda pricing
  - **Context-Specific Tracking**: Separate cost attribution for each processing step enabling granular cost analysis per document processing context
  - **Automatic Integration**: Lambda costs automatically integrate with existing cost reporting infrastructure and appear alongside AWS service costs (Textract, Bedrock, SageMaker)
  - **Configuration Integration**: Added Lambda pricing entries to all 7 configuration files in `config_library/` using official US East pricing

### Fixed

- Defect in v0.3.17 causing workflow tracker failure to (1) update status of failed workflows, and (2) update reporting database for all workflows #72

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.3.18.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.3.18.yaml`

## [0.3.17]

### Added

- **Edit Sections Feature for Modifying Class/Type and Reprocessing Extraction**
  - Added Edit Sections interface for Pattern-2 and Pattern-3 workflows with reprocessing optimization
  - **Key Features**: Section management (create, update, delete), classification updates, page reassignment with overlap detection, real-time validation
  - **Selective Reprocessing**: Only modified sections are reprocessed while preserving existing data for unmodified sections
  - **Processing Pipeline**: All functions (OCR/Classification/Extraction/Assessment) automatically skip redundant operations based on data presence
  - **Pattern Compatibility**: Full functionality for Pattern-2/Pattern-3, informative modal for Pattern-1 explaining BDA not yet supported

- **Analytics Agent Schema Optimization for Improved Performance**
  - **Embedded Database Overview**: Complete table listing and guidance embedded directly in system prompt (no tool call needed)
  - **On-Demand Detailed Schemas**: `get_table_info(['specific_tables'])` loads detailed column information only for tables actually needed by the query
  - **Significant Performance Gains**: Eliminates redundant tool calls on every query while maintaining token efficiency
  - **Enhanced SQL Guidance**: Comprehensive Athena/Trino function reference with explicit PostgreSQL operator warnings to prevent common query failures like `~` regex operator mistakes
  - **Faster Time-to-Query**: Agent has immediate access to table overview and can proceed directly to detailed schema loading for relevant tables

### Changed

- Add UI code lint/validation to publish.py script

### Fixed

- Fix missing data in Glue tables when using a document class that contains a dash (-).
- Added optional Bedrock Guardrails support to (a) Agent Analytics and (b) Chat with Document
- Fixed regressions on Permission Boundary support for all roles, and added autimated tests to prevent recurrance - fixes #70

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.3.17.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.3.17.yaml`

## [0.3.16]

### Added

- **S3 Vectors Support for Cost-Optimized Knowledge Base Storage**
  - Added S3 Vectors as alternative vector store option to OpenSearch Serverless for Bedrock Knowledge Base with lower storage costs
  - Custom resource Lambda implementation for S3 vector bucket and index management (using boto3 s3vectors client) with proper IAM permissions and resource cleanup
  - Unified Knowledge Base interface supporting both vector store types with automatic resource provisioning based on user selection

- **Page Limit Configuration for Classification Control**
  - Added `maxPagesForClassification` configuration option to control how many pages are used during document classification
  - **Default Behavior**: `"ALL"` - uses all pages for classification (existing behavior)
  - **Limited Page Classification**: Set to numeric value (e.g., `"1"`, `"2"`, `"3"`) to classify only the first N pages
  - **Important**: When using numeric limit, the classification result from the first N pages is applied to ALL pages in the document, effectively forcing the entire document to be assigned a single class with one section
  - **Use Cases**: Performance optimization for large documents, cost reduction for documents with consistent classification patterns, simplified processing for homogeneous document types

- **CloudFormation Service Role for Delegated Deployment Access**
  - Added example CloudFormation service role template that enables non-administrator users to deploy and maintain IDP stacks without requiring ongoing administrator permissions
  - Administrators can provision the service role once with elevated privileges, then delegate deployment capabilities to developer/DevOps teams
  - Includes comprehensive documentation and cross-referenced deployment guides explaining the security model and setup process

### Fixed

- Fixed issue where CloudFront policy statements were still appearing in generated GovCloud templates despite CloudFront resources being removed
- Fix duplicate Glue tables are created when using a document class that contains a dash (-). Resolved by replacing dash in section types with underscore character when creating the table, to align with the table name generated later by the Glue crawler - resolves #57.
- Fix occasional UI error 'Failed to get document details - please try again later' - resolves #58
- Fixed UI zipfile creation to exclude .aws-sam directories and .env files from deployment package
- Added security recommendation to set LogLevel parameter to WARN or ERROR (not INFO) for production deployments to prevent logging of sensitive information including PII data, document contents, and S3 presigned URLs
- Hardened several aspects of the new Discovery feature

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.3.16.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.3.16.yaml`

## [0.3.15]

### Added

- **Intelligent Document Discovery Module for Automated Configuration Generation**
  - Added Discovery module that automatically analyzes document samples to identify structure, field types, and organizational patterns
  - **Pattern-Neutral Design**: Works across all processing patterns (1, 2, 3) with unified discovery process and pattern-specific implementations
  - **Dual Discovery Methods**: Discovery without ground truth (exploratory analysis) and with ground truth (optimization using labeled data)
  - **Automated Blueprint Creation**: Pattern 1 includes zero-touch BDA blueprint generation with intelligent change detection and version management
  - **Web UI Integration**: Real-time discovery job monitoring, interactive results review, and seamless configuration integration
  - **Advanced Features**: Multi-model support (Nova, Claude), customizable prompts, configurable parameters, ground truth processing, schema conversion, and lifecycle management
  - **Key Benefits**: Rapid new document type onboarding, reduced time-to-production, configuration optimization, and automated workflow bootstrapping
  - **Use Cases**: New document exploration, configuration improvement, rapid prototyping, and document understanding
  - **Documentation**: Guide in `docs/discovery.md` with architecture details, best practices, and troubleshooting

- **Optional Pattern-2 Regex-Based Classification for Enhanced Performance**
  - Added support for optional regex patterns in document class definitions for performance optimization
  - **Document Name Regex**: Match against document ID/name to classify all pages without LLM processing when all pages should be the same class
  - **Document Page Content Regex**: Match against page text content during multi-modal page-level classification for fast page classification
  - **Key Benefits**: Significant performance improvements and cost savings by bypassing LLM calls for pattern-matched documents, deterministic classification results for known document patterns, seamless fallback to existing LLM classification when regex patterns don't match
  - **Configuration**: Optional `document_name_regex` and `document_page_content_regex` fields in class definitions with automatic regex compilation and validation
  - **Logging**: Comprehensive info-level logging when regex patterns match for observability and debugging
  - **CloudFormation Integration**: Updated Pattern-2 schema to support regex configuration through the Web UI
  - **Demonstration**: New `step2_classification_with_regex.ipynb` notebook showcasing regex configuration and performance comparisons
  - **Documentation**: Enhanced classification module README and main documentation with regex usage examples and best practices
- **Windows WSL Development Environment Setup Guide**
  - Added WSL-based development environment setup guide for Windows developers in `docs/setup-development-env-WSL.md`
  - **Key Features**: Automated setup script (`wsl_setup.sh`) for quick installation of Git, Python, Node.js, AWS CLI, and SAM CLI
  - **Integrated Workflow**: Development setup combining Windows tools (VS Code, browsers) with native Linux environment
  - **Target Use Cases**: Windows developers needing Linux compatibility without Docker Desktop or VM overhead

### Fixed

- **Throttling Error Detection and Retry Logic for Assessment Functions** - [GitHub Issue #45](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/45)
  - **Assessment Function**: Enhanced throttling detection to check for throttling errors returned in `document.errors` field in addition to thrown exceptions, raising `ThrottlingException` to trigger Step Functions retry when throttling is detected
  - **Granular Assessment Task Caching**: Fixed caching logic to properly cache successful assessment tasks when there are ANY failed tasks (both exception-based and result-based failures), enabling efficient retry optimization by only reprocessing failed tasks while preserving successful results
  - **Impact**: Improved resilience for throttling scenarios, reduced redundant processing during retries, and better Step Functions retry behavior

- **Security Vulnerability Mitigation - Package Updates**

- **GovCloud Compatibility - Hardcoded Service Domain References**
  - Fixed hardcoded `amazonaws.com` references in CloudFormation templates that prevented GovCloud deployment
  - Updated all service principals and endpoints to use dynamic `${AWS::URLSuffix}` expressions for automatic region-based resolution
  - **Templates Updated**: `template.yaml` (main template), `patterns/pattern-3/sagemaker_classifier_endpoint.yaml`
  - **Services Fixed**: EventBridge, Cognito, SageMaker, ECR, CloudFront, CodeBuild, AppSync, Lambda, DynamoDB, CloudWatch Logs, Glue
  - Resolves GitHub Issue #50 - templates now deploy correctly in both standard AWS and GovCloud regions

- **Bug Fixes and Code Improvements**
  - Fixed HITL processing errors in both Pattern-1 (DynamoDB validation with empty strings) and Pattern-2 (string indices error in A2I output processing)
  - Fixed Step Function UI issues including auto-refresh button auto-disable and fetch failures for failed executions with datetime serialization errors
  - Cleaned up unused Step Function subscription infrastructure and removed duplicate code in Pattern-2 HITL function
  - Expanded UI Visual Editor bounding box size with padding for better visibility and user interaction
  - Fixed bug in list of models supporting cache points - previously claude 4 sonnet and opus had been excluded.
  - Validations added at the assessment step for checking valid json response. The validation fails after extraction/assessment is complete if json parsing issues are encountered.

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.3.15.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.3.15.yaml`

## [0.3.14]

### Added

- Support for 1m token context for Claude Sonnet 4
- Video demo of "Chat with Document" in [./docs/web-ui.md](./docs/web-ui.md)
- **Human-in-the-Loop (HITL) Support Extended to Pattern-2**
  - Added HITL review capabilities for Pattern-2 (Textract + Bedrock processing) using Amazon SageMaker Augmented AI (A2I)
  - Enables human validation and correction when extraction confidence falls below configurable threshold
  - Includes same features as Pattern-1 HITL: automatic triggering, review portal integration, and seamless result updates
  - Documentation and video demo in [./docs/human-review.md](./docs/human-review.md)

### Removed

- Windows development environment guide and setup script removed as it proved insufficiently robust

### Fixed

- Fix 1-click Launch URL output from the GovCloud template generation script
- Add Agent Analytics to architecture diagram
- Fix various UX and error reporting issues with the new Python publish script
- Simplify UDOP model path construction and avoid invalid default for regions other than us-east-1 and us-west-2
- Permission regression from previous release affecting "Chat with Document"

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.3.14.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.3.14.yaml`

## [0.3.13]

### Added

- **External MCP Agent Integration for Custom Tool Extension**
  - Added External MCP (Model Context Protocol) Agent support that enables integration with custom MCP servers to extend IDP capabilities
  - **Cross-Account Integration**: Host MCP servers in separate AWS accounts or external infrastructure with secure OAuth authentication using AWS Cognito
  - **Dynamic Tool Discovery**: Automatically discovers and integrates available tools from MCP servers through the IDP web interface
  - **Secure Authentication Flow**: Uses AWS Cognito User Pools for OAuth bearer token authentication with proper token validation
  - **Configuration Management**: JSON array configuration in AWS Secrets Manager supporting multiple MCP server connections with optional custom agent names and descriptions
  - **Real-time Integration**: Tools become immediately available through the IDP web interface after configuration

- **AWS GovCloud Support with Automated Template Generation**
  - Added GovCloud compatibility through `scripts/generate_govcloud_template.py` script
  - **ARN Partition Compatibility**: All templates updated to use `arn:${AWS::Partition}:` for both commercial and GovCloud regions
  - **Headless Operation**: Automatically removes UI-related resources (CloudFront, AppSync, Cognito, WAF) for GovCloud deployment
  - **Core Functionality Preserved**: All 3 processing patterns and complete 6-step pipeline (OCR, Classification, Extraction, Assessment, Summarization, Evaluation) remain fully functional
  - **Automated Workflow**: Single script orchestrates build + GovCloud template generation + S3 upload with deployment URLs
  - **Enterprise Ready**: Enables headless document processing for government and enterprise environments requiring GovCloud compliance
  - **Documentation**: New `docs/govcloud-deployment.md` with deployment guide, architecture differences, and access methods

- **Pattern-2 and Pattern-3 Assessment now generate geometry (bounding boxes) for visualization in UI 'Visual Editor' (parity with Pattern-1)**
  - Added comprehensive spatial localization capabilities to both regular and granular assessment services
  - **Automatic Processing**: When LLM provides bbox coordinates, automatically converts to UI-compatible (Visual Edit) geometry format without any configuration
  - **Universal Support**: Works with all attribute types - simple attributes, nested group attributes (e.g., CompanyAddress.State), and list attributes
  - **Enhanced Prompts**: Updated assessment task prompts with spatial-localization-guidelines requesting bbox coordinates in normalized 0-1000 scale
  - **Demo Notebooks**: Assessment notebooks now showcase automatic bounding box processing

- **New Python-Based Publishing System**
  - Replaced `publish.sh` bash script with new `publish.py` Python script
  - Rich console interface with progress bars, spinners, and colored output using Rich library
  - Multi-threaded artifact building and uploading for significantly improved performance
  - Native support for Linux, macOS, and Windows environments

- **Windows Development Environment Setup Guide and Helper Script**
  - New `scripts/dev_setup.bat` (570 lines) for complete Windows development environment configuration

- **OCR Service Default Image Sizing for Resource Optimization**
  - Implemented automatic default image size limits (951×1268) when no image sizing configuration is provided
  - **Key Benefits**: Reduction in vision model token consumption, prevents OutOfMemory errors during concurrent processing, improves processing speed and reduces bandwidth usage

### Changed

- **Reverted to python3.12 runtime to resolve build package dependency problems**

### Fixed

- **Improved Visual Edit bounding box position when using image zoom or pan**

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.3.13.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.3.13.yaml`

## [0.3.12]

### Added

- **Custom Prompt Generator Lambda Support for Patterns 2 & 3**
  - Added `custom_prompt_lambda_arn` configuration field to enable injection of custom business logic into extraction processing
  - **Key Features**: Lambda interface with all template placeholders (DOCUMENT_TEXT, DOCUMENT_CLASS, ATTRIBUTE_NAMES_AND_DESCRIPTIONS, DOCUMENT_IMAGE), URI-based image handling for JSON serialization, comprehensive error handling with fail-fast behavior, scoped IAM permissions requiring GENAIIDP-\* function naming
  - **Use Cases**: Document type-specific processing rules, integration with external systems for customer configurations, conditional processing based on document content, regulatory compliance and industry-specific requirements
  - **Demo Resources**: Interactive notebook demonstration (`step3_extraction_with_custom_lambda.ipynb`), SAM deployment template for demo Lambda function, comprehensive documentation and examples in `notebooks/examples/demo-lambda/`
  - **Benefits**: Custom business logic without core code changes, backward compatible (existing deployments unchanged), robust JSON serialization handling all object types, complete observability with detailed logging

- **Refactored Document Classification Service for Enhanced Boundary Detection**
  - Consolidated `multimodalPageLevelClassification` and the experimental `multimodalPageBoundaryClassification` (from v0.3.11) into a single enhanced `multimodalPageLevelClassification` method
  - Implemented BIO-like sequence segmentation with document boundary indicators: "start" (new document) and "continue" (same document)
  - Automatically segments multi-document packets, even when they contain multiple documents of the same type
  - Added comprehensive classification guide with method comparisons and best practices
  - **Benefits**: Simplified codebase with single multimodal classification method, improved handling of complex document packets, maintains backward compatibility
  - **No Breaking Changes**: Existing configurations work unchanged, no configuration updates required

- **Enhanced A2I Template and Workflow Management**
  - Enhanced A2I template with improved user interface and clearer instructions for reviewers
  - Added comprehensive instructions for reviewers in A2I template to guide the review process
  - Implemented capture of failed review tasks with proper error handling and logging
  - Added workflow orchestration control to stop processing when reviewer rejects A2I task
  - Removed automatic A2I task creation when Pattern-1 Bedrock Data Automation (BDA) fails to classify document to appropriate Blueprint

- **Dynamic Cost Calculation for Metering Data**
  - Added automated unit cost and estimated cost calculation to metering table with new `unit_cost` and `estimated_cost` columns
  - Dynamic pricing configuration loading from configuration
  - Enhanced cost analysis capabilities with comprehensive Athena queries for cost tracking, trend analysis, and efficiency metrics
  - Automatic cost calculation as `estimated_cost = value × unit_cost` for all metering records
- **Configuration-Based Summarization Control**
  - Summarization can now be enabled/disabled via configuration file `summarization.enabled` property instead of CloudFormation stack parameter
  - **Key Benefits**: Runtime control without stack redeployment, zero LLM costs when disabled, simplified state machine architecture, backward compatible defaults
  - **Implementation**: Always calls SummarizationStep but service skips processing when `enabled: false`
  - **Cost Optimization**: When disabled, no LLM API calls or S3 operations are performed
  - **Configuration Example**: Set `summarization.enabled: false` to disable, `enabled: true` to enable (default)

- **Configuration-Based Assessment Control**
  - Assessment can now be enabled/disabled via configuration file `assessment.enabled` property instead of CloudFormation stack parameter
  - **Key Benefits**: Runtime control without stack redeployment, zero LLM costs when disabled, simplified state machine architecture, backward compatible defaults
  - **Implementation**: Always calls AssessmentStep but service skips processing when `enabled: false`
  - **Cost Optimization**: When disabled, no LLM API calls or S3 operations are performed
  - **Configuration Example**: Set `assessment.enabled: false` to disable, `enabled: true` to enable (default)

- **New guides for setting up development environments**
  - EC2-based Linux development environment
  - MacOS development environment

### Removed

- **CloudFormation Parameters**: Removed `IsSummarizationEnabled` and `IsAssessmentEnabled` parameters from all pattern templates
- **Related Conditions**: Removed parameter conditions and state machine definition substitutions for both features
- **Conditional Logic**: Eliminated complex conditional logic from state machine definitions for summarization and assessment steps

### ⚠️ Breaking Changes

- **Configuration Migration Required**: When updating a stack that previously had `IsSummarizationEnabled` or `IsAssessmentEnabled` set to `false`, these features will now default to `enabled: true` after the update. To maintain the disabled behavior:
  1. Update your configuration file to set `summarization.enabled: false` and/or `assessment.enabled: false` as needed
  2. Save the configuration changes immediately after the stack update
  3. This ensures continued cost optimization by preventing unexpected LLM API calls
- **Action Required**: Review your current CloudFormation parameter settings before updating and update your configuration accordingly to preserve existing behavior

### Changed

- **Updated Python Lambda Runtime to 3.13**

### Fixed

- **Fixed B615 "Unsafe Hugging Face Hub download without revision pinning" security finding in Pattern-3 fine-tuning module** - Added revision pinning with to prevent supply chain attacks and ensure reproducible deployments
- **Fixed CloudWatch Log Group Missing Retention regression**
- **Security: Cross-Site Scripting (XSS) Vulnerability in FileViewer Component** - Fixed high-risk XSS vulnerability in `src/ui/src/components/document-viewer/FileViewer.jsx` where `innerHTML` was used with user-controlled data
- **Add permissions boundary support to new Lambda function roles introduced in previous releases**
- **Fixed OutOfMemory Errors in Pattern-2 OCR Lambda for Large High-Resolution Documents**
  - **Root Cause**: Processing large PDFs with high-resolution images (7469×9623 pixels) caused memory spikes when 20 concurrent workers each held ~101MB images simultaneously, exceeding the 4GB Lambda memory limit
  - **Optimal Solution**: Refactored image extraction to render directly at target dimensions using PyMuPDF matrix transformations, completely eliminating oversized image creation

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.3.12.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.3.12.yaml`

## [0.3.11]

### Added

- **Chat with Document** now available at the bottom of the each Document Detail page.
- **Anthropic Claude Opus 4.1** model available in configuration for all document processing steps
- **Browser tab icon** now features a blue background with a white "IDP"
- **Experimental new classification method** - multimodalPageBoundaryClassification - for detecting section boundaries during page level classification.

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.3.11.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.3.11.yaml`

## [0.3.10]

### Added

- **Agent Analysis Feature for Natural Language Document Analytics**
  - Added integrated AI-powered analytics agent that enables natural language querying of processed document data
  - **Key Capabilities**: Convert natural language questions to SQL queries, generate interactive visualizations and tables, explore database schema automatically
  - **Secure Architecture**: All Python code execution happens in isolated AWS Bedrock AgentCore sandboxes, not in Lambda functions
  - **Multi-Tool Agent System**: Database discovery tool for schema exploration, Athena query tool for SQL execution, secure code sandbox for data transfer, Python visualization tool for charts and tables
  - **Example Use Cases**: Query document processing volumes and trends, analyze confidence scores and extraction accuracy, explore document classifications and content patterns, generate custom charts and data tables
  - **Sample W2 Test Data**: Includes 20 synthetic W2 tax documents for testing analytics capabilities
  - **Configurable Models**: Supports multiple AI models including Claude 3.7 Sonnet (default), Claude 3.5 Sonnet, Nova Pro/Lite, and Haiku
  - **Web UI Integration**: Accessible through "Document Analytics" section with real-time progress display and query history

- **Automatic Glue Table Creation for Document Sections**
  - Added automatic creation of AWS Glue tables for each document section type (classification) during processing
  - Tables are created dynamically when new section types are encountered, eliminating manual table creation
  - Consistent lowercase naming convention for tables ensures compatibility with case-sensitive S3 paths
  - Tables are configured with partition projection for efficient date-based queries without manual partition management
  - Automatic schema evolution - tables update when new fields are detected in extraction results

### Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.3.10.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.3.10.yaml`

## [0.3.9]

### Added

- **Optional Permissions Boundary Support for Enterprise Deployments**
  - Added `PermissionsBoundaryArn` parameter to all CloudFormation templates for organizations with Service Control Policies (SCPs) requiring permissions boundaries
  - Comprehensive support for both explicit IAM roles and implicit roles created by AWS SAM functions and statemachines`
  - Conditional implementation ensures backward compatibility - when no permissions boundary is provided, roles deploy normally

### Added

- IDP Configuration and Prompting Best Practices documentation [doc](./docs/idp-configuration-best-practices.md)

### Changed

- Updated lending_package.pdf sample with more realistic driver's license image

### Fixed

- Issue #27 - removed idp_common bedrock client region default to us-west-2 - PR #28

## [0.3.8]

### Added

- **Lending Package Configuration Support for Pattern-2**
  - Added new `lending-package-sample` configuration to Pattern-2, providing comprehensive support for lending and financial document processing workflows
  - New default configuration for Pattern-2 stack deployments, optimized for loan applications, mortgage processing, and financial verification documents
  - Previous `rvl-cdip-sample` configuration remains available by selecting `rvl-cdip` for the `Pattern2Configuration` parameter when deploying or updating stacks

- **Text Confidence View for Document Pages**
  - Added support for displaying OCR text confidence data through new `TextConfidenceUri` field
  - New "Text Confidence View" option in the UI pages panel alongside existing Markdown and Text views
  - Fixed issues with view persistence - Text Confidence View button now always visible with appropriate messaging when content unavailable
  - Fixed view toggle behavior - switching between views no longer closes the viewer window
  - Reordered view buttons to: Markdown View, Text Confidence View, Text View for better user experience

- **Enhanced OCR DPI Configuration for PDF files**
  - DPI for PDF image conversion is now configurable in the configuration editor under OCR image processing settings
  - Default DPI improved from 96 to 150 DPI for better default quality and OCR accuracy
  - Configurable through Web UI without requiring code changes or redeployment

### Changed

- **Converted text confidence data format from JSON to markdown table for improved readability and reduced token usage**
  - Removed unnecessary "page_count" field
  - Changed "text_blocks" array to "text" field containing a markdown table with Text and Confidence columns
  - Reduces prompt size for assessment service while improving UI readability
  - OCR confidence values now rounded to 1 decimal point (e.g., 99.1, 87.3) for cleaner display
  - Markdown table headers now explicitly left-aligned using `|:-----|:-----------|` format for consistent appearance

- **Simplified OCR Service Initialization**
  - OCR service now accepts a single `config` dictionary parameter for cleaner, more consistent API
  - Aligned with classification service pattern for better consistency across IDP services
  - Backward compatibility maintained - old parameter pattern still supported with deprecation warning
  - Updated all lambda functions and notebooks to use new simplified pattern
- Removed fixed image target_height and target_width from default configurations, so images are processed in original resolution by default.

- **Updated Default Configuration for Pattern1 and Pattern2**
  - Changed default configuration for new stacks from "default" to "lending-package-sample" for both Pattern1 and Pattern2
  - Maintains backward compatibility for stack updates by keeping the parameter value "default" mapped to the rvl-cdip-sample for pattern-2.

- **Reduce assessment step costs**
  - Default model for granular assessment is now `us.amazon.nova-lite-v1:0` - experimentation recommended
  - Improved placement of <<CACHEPOINT>> tags in assessment prompt to improve utilization of prompt caching

### Fixed

- **Fixed Image Resizing Behavior for High-Resolution Documents**
  - Fixed issue where empty strings in image configuration were incorrectly resizing images to default 951x1268 pixels instead of preserving original resolution
  - Empty strings (`""`) in `target_width` and `target_height` configuration now preserve original document resolution for maximum processing accuracy
- Fixed issue where PNG files were being unnecessarily converted to JPEG format and resized to lower resolution with lost quality
- Fixed issue where PNG and JPG image files were not rendering inline in the Document Details page
- Fixed issue where PDF files were being downloaded instead of displayed inline
- Fixed pricing data for cacheWrite tokens for Amazon Nova models to resolve innacurate cost estimation in UI.

## [0.3.7]

### Added

- **Criteria Validation Service Class**
  - New document validation service that evaluates documents against dynamic business rules using Large Language Models (LLMs)
  - **Key Capabilities**: Dynamic business rules configuration, asynchronous processing with concurrent criteria evaluation, intelligent text chunking for large documents, multi-file processing with summarization, comprehensive cost and performance tracking
  - **Primary Use Cases**: Healthcare prior authorization workflows, compliance validation, business rule enforcement, quality assurance, and audit preparation
  - **Architecture Features**: Seamless integration with IDP pipeline using common Bedrock client, unified metering with automatic token usage tracking, S3 operations using standardized file operations, configuration compatibility with existing IDP config system
  - **Advanced Features**: Configurable criteria questions without code changes, robust error handling with graceful degradation, Pydantic-based input/output validation with automatic data cleaning, comprehensive timing metrics and token usage tracking
  - **Limitation**: Python idp_common support only, not yet implemented within deployed pattern workflows.

- **Document Process Flow Visualization**
  - Added interactive visualization of Step Functions workflow execution for document processing
  - Visual representation of processing steps with status indicators and execution details
  - Detailed step information including inputs, outputs, and error messages
  - Timeline view showing chronological execution of all processing steps
  - Auto-refresh capability for monitoring active executions in real-time
  - Support for Map state visualization with iteration details
  - Error diagnostics with detailed error messages for troubleshooting
  - Automatic selection of failed steps for quick issue identification

- **Granular Assessment Service for Scalable Confidence Evaluation**
  - New granular assessment approach that breaks down assessment into smaller, focused tasks for improved accuracy and performance
  - **Key Benefits**: Better accuracy through focused prompts, cost optimization via prompt caching, reduced latency through parallel processing, and scalability for complex documents
  - **Task Types**: Simple batch tasks (groups 3-5 simple attributes), group tasks (individual group attributes), and list item tasks (individual list items for maximum accuracy)
  - **Configuration**: Configurable batch sizes (`simple_batch_size`, `list_batch_size`) and parallel processing (`max_workers`) for performance tuning
  - **Prompt Caching**: Leverages LLM caching capabilities with cached base content (document context, images, OCR data) and dynamic task-specific content
  - **Use Cases**: Ideal for bank statements with hundreds of transactions, documents with 10+ attributes, complex nested structures, and performance-critical scenarios
  - **Backward Compatibility**: Maintains same interface as standard assessment service with seamless migration path
  - **Enhanced Documentation**: Comprehensive documentation in `docs/assessment.md` and example notebooks for both standard and granular approaches

- **Reporting Database now has Document Sections Tables to enable querying across document fields**
  - Added comprehensive document sections storage system that automatically creates tables for each section type (classification)
  - **Dynamic Table Creation**: AWS Glue Crawler automatically discovers new section types and creates corresponding tables (e.g., `invoice`, `receipt`, `bank_statement`)
  - **Configurable Crawler Schedule**: Support for manual, every 15 minutes, hourly, or daily (default) crawler execution via `DocumentSectionsCrawlerFrequency` parameter
  - **Partitioned Storage**: Data organized by section type and date for efficient querying with Amazon Athena

- **Partition Projections for Evaluation and Metering tables**
  - **Automated Partition Management**: Eliminates need for `MSCK REPAIR TABLE` operations with projection-based partition discovery
  - **Performance Benefits**: Athena can efficiently prune partitions based on date ranges without manual partition loading
  - **Backward Compatibility Warning**: The partition structure change from `year=2024/month=03/day=15/` to `date=2024-03-15/` means that data saved in the evaluation or metering tables prior to v0.3.7 will not be visible in Athena queries after updating. To retain access to historical data, you can either:
    - Manually reorganize existing S3 data to match the new partition structure
    - Create separate Athena tables pointing to the old partition structure for historical queries

- **Optimize the classification process for single class configurations in Pattern-2**
  - Detects when only a single document class is defined in the configuration
  - Automatically classifies all document pages as that single class
  - Creates a single section containing all pages
  - Bypasses the backend service calls (Bedrock or SageMaker) completely
  - Logs an INFO message indicating the optimization is active

- **Skip the extraction process for classes with no attributes in Pattern 2/3**
  - Add early detection logic in extraction class to check for empty/missing attributes
  - Return zero metering data and empty JSON results when no attributes defined

- **Enhanced State Machine Optimization for Very Large Documents**
  - Improved document compression to store only section IDs rather than full section objects
  - Modified state machine workflow to eliminate nested result structures and reduce payload size
  - Added OutputPath filtering to remove intermediate results from state machine execution
  - Streamlined assessment step to replace extraction results instead of nesting them
  - Resolves "size exceeding the maximum number of bytes service limit" errors for documents with 500+ pages

### Changed

- **Default behavior for image attachment in Pattern-2 and Pattern3**
  - If the prompt contains a `{DOCUMENT_IMAGE}` placeholder, keep the current behavior (insert image at placeholder)
  - If the prompt does NOT contain a `{DOCUMENT_IMAGE}` placeholder, do NOT attach the image at all
  - Previously, if the (classification or extraction) prompt did NOT contain a `{DOCUMENT_IMAGE}` placeholder, the image was appended at the end of the content array anyway
- **Modified default assessment prompt for token efficiency**
  - Removed `confidence_reason` from output to avoid consuming unnecessary output tokens
  - Refactored task_prompt layout to improve <<CACHEPOINT>> placement for efficiency when granular mode is enabled or disabled
- **Enhanced .clinerules with comprehensive memory bank workflows**
  - Enhanced Plan Mode workflow with requirements gathering, reasoning, and user approval loop

### Fixed

- Fixed UI list deletion issue where empty lists were not saved correctly - #18
- Improve structure and clarity for idp_common Python package documentation
- Improved UI in View/Edit Configuration to clarify that Class and Attribute descriptions are used in the classification and extraction prompts
- Automate UI updates for field "HITL (A2I) Status" in the Document list and document details section.
- Fixed image display issue in PagesPanel where URLs containing special characters (commas, spaces) would fail to load by properly URL-encoding S3 object keys in presigned URL generation

## [0.3.6]

### Fixed

- Update Athena/Glue table configuration to use Parquet format instead of JSON #20
- Cloudformation Error when Changing Evaluation Bucket Name #19

### Added

- **Extended Document Format Support in OCR Service**
  - Added support for processing additional document formats beyond PDF and images:
    - Plain text (.txt) files with automatic pagination for large documents
    - CSV (.csv) files with table visualization and structured output
    - Excel workbooks (.xlsx, .xls) with multi-sheet support (each sheet as a page)
    - Word documents (.docx, .doc) with text extraction and visual representation
  - **Key Features**:
    - Consistent processing model across all document formats
    - Standard page image generation for all formats
    - Structured text output in formats compatible with existing extraction pipelines
    - Confidence metrics for all document types
    - Automatic format detection from file content and extension
  - **Implementation Details**:
    - Format-specific processing strategies for optimal results
    - Enhanced text rendering for plain text documents
    - Table visualization for CSV and Excel data
    - Word document paragraph extraction with formatting preservation
    - S3 storage integration matching existing PDF processing workflow

## [0.3.5]

### Added

- **Human-in-the-Loop (HITL) Support - Pattern 1**
  - Added comprehensive Human-in-the-Loop review capabilities using Amazon SageMaker Augmented AI (A2I)
  - **Key Features**:
    - Automatic triggering when extraction confidence falls below configurable threshold
    - Integration with SageMaker A2I Review Portal for human validation and correction
    - Configurable confidence threshold through Web UI Portal Configuration tab (0.0-1.0 range)
    - Seamless result integration with human-verified data automatically updating source results
  - **Workflow Integration**:
    - HITL tasks created automatically when confidence thresholds are not met
    - Reviewers can validate correct extractions or make necessary corrections through the Review Portal
    - Document processing continues with human-verified data after review completion
  - **Configuration Management**:
    - `EnableHITL` parameter for feature toggle
    - Confidence threshold configurable via Web UI without stack redeployment
    - Support for existing private workforce work teams via input parameter
  - **CloudFormation Output**: Added `SageMakerA2IReviewPortalURL` for easy access to review portal
  - **Known Limitations**: Current A2I version cannot provide direct hyperlinks to specific document tasks; template updates require resource recreation
- **Document Compression for Large Documents - all patterns**
  - Added automatic compression support to handle large documents and avoid exceeding Step Functions payload limits (256KB)
  - **Key Features**:
    - Automatic compression (default trigger threshold of 0KB enables compression by default)
    - Transparent handling of both compressed and uncompressed documents in Lambda functions
    - Temporary S3 storage for compressed document state with automatic cleanup via lifecycle policies
  - **New Utility Methods**:
    - `Document.load_document()`: Automatically detects and decompresses document input from Lambda events
    - `Document.serialize_document()`: Automatically compresses large documents for Lambda responses
    - `Document.compress()` and `Document.decompress()`: Compression/decompression methods
  - **Lambda Function Integration**: All relevant Lambda functions updated to use compression utilities
  - **Resolves Step Functions Errors**: Eliminates "result with a size exceeding the maximum number of bytes service limit" errors for large multi-page documents
- **Multi-Backend OCR Support - Pattern 2 and 3**
  - Textract Backend (default): Existing AWS Textract functionality
  - Bedrock Backend: New LLM-based OCR using Claude/Nova models
  - None Backend: Image-only processing without OCR
- **Bedrock OCR Integration - Pattern 2 and 3**
  - Customizable system and task prompts for OCR optimization
  - Better handling of complex documents, tables, and forms
  - Layout preservation capabilities
- **Image Preprocessing - Pattern 2**
  - Adaptive Binarization: Improves OCR accuracy on documents with:
    - Uneven lighting or shadows
    - Low contrast text
    - Background noise or gradients
  - Optional feature with configurable enable/disable
- **YAML Parsing Support for LLM Responses - Pattern 2 and 3**
  - Added comprehensive YAML parsing capabilities to complement existing JSON parsing functionality
  - New `extract_yaml_from_text()` function with robust multi-strategy YAML extraction:
    - YAML in `yaml and`yml code blocks
    - YAML with document markers (---)
    - Pattern-based YAML detection using indentation and key indicators
  - New `detect_format()` function for automatic format detection returning 'json', 'yaml', or 'unknown'
  - New unified `extract_structured_data_from_text()` wrapper function that automatically detects and parses both JSON and YAML formats
  - **Token Efficiency**: YAML typically uses 10-30% fewer tokens than equivalent JSON due to more compact syntax
  - **Service Integration**: Updated classification service to use the new unified parsing function with automatic fallback between formats
  - **Comprehensive Testing**: Added 39 new unit tests covering all YAML extraction strategies, format detection, and edge cases
  - **Backward Compatibility**: All existing JSON functionality preserved unchanged, new functionality is purely additive
  - **Intelligent Fallback**: Robust fallback mechanism handles cases where preferred format fails (e.g., JSON requested as YAML falls back to JSON)
  - **Production Ready**: Handles malformed content gracefully, comprehensive error handling and logging
  - **Example Notebook**: Added `notebooks/examples/step3_extraction_using_yaml.ipynb` demonstrating YAML-based extraction with automatic format detection and token efficiency benefits

### Fixed

- **Enhanced JSON Extraction from LLM Responses (Issue #16)**
  - Modularized duplicate `_extract_json()` functions across classification, extraction, summarization, and assessment services into a common `extract_json_from_text()` utility function
  - Improved multi-line JSON handling with literal newlines in string values that previously caused parsing failures
  - Added robust JSON validation and multiple fallback strategies for better extraction reliability
  - Enhanced string parsing with proper escape sequence handling for quotes and newlines
  - Added comprehensive unit tests covering various JSON formats including multi-line scenarios

## [0.3.4]

### Added

- **Configurable Image Processing and Enhanced Resizing Logic**
  - **Improved Image Resizing Algorithm**: Enhanced aspect-ratio preserving scaling that only downsizes when necessary (scale factor < 1.0) to prevent image distortion
  - **Configurable Image Dimensions**: All processing services (Assessment, Classification, Extraction, OCR) now support configurable image dimensions through configuration with default 951×1268 resolution
  - **Service-Specific Image Optimization**: Each service can use optimal image dimensions for performance and quality tuning
  - **Enhanced OCR Service**: Added configurable DPI for PDF-to-image conversion and optional image resizing with dual image strategy (stores original high-DPI images while using resized images for processing)
  - **Runtime Configuration**: No code changes needed to adjust image processing - all configurable through service configuration
  - **Backward Compatibility**: Default values maintain existing behavior with no immediate action required for existing deployments
- **Enhanced Configuration Management**
  - **Save as Default**: New button to save current configuration as the new default baseline with confirmation modal and version upgrade warnings
  - **Export Configuration**: Export current configuration to local files in JSON or YAML format with customizable filename
  - **Import Configuration**: Import configuration from local JSON or YAML files with automatic format detection and validation
  - Enhanced Lambda resolver with deep merge functionality for proper default configuration updates
  - Automatic custom configuration reset when saving as default to maintain clean state
- **Nested Attribute Groups and Lists Support**
  - Enhanced document configuration schema to support complex nested attribute structures with three attribute types:
    - **Simple attributes**: Single-value extractions (existing behavior)
    - **Group attributes**: Nested object structures with sub-attributes (e.g., address with street, city, state)
    - **List attributes**: Arrays with item templates containing multiple attributes per item (e.g., transactions with date, amount, description)
  - **Web UI Enhancements**: Configuration editor now supports viewing and editing nested attribute structures with proper validation
  - **Extraction Service Updates**: Enhanced `{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}` placeholder processing to generate formatted prompts for nested structures
  - **Assessment Service Enhancements**: Added support for nested structure confidence evaluation with recursive processing of group and list attributes, including proper confidence threshold application from configuration
  - **Evaluation Service Improvements**:
    - Implemented pattern matching for list attributes (e.g., `Transactions[].Date` maps to `Transactions[0].Date`, `Transactions[1].Date`)
    - Added data flattening for complex extraction results using dot notation and array indices
    - Fixed numerical sorting for list items (now sorts 0, 1, 2, ..., 10, 11 instead of alphabetically)
    - Individual evaluation methods applied per nested attribute (EXACT, FUZZY, SEMANTIC, etc.)
  - **Documentation**: Comprehensive updates to evaluation docs and README files with nested structure examples and processing explanations
  - **Use Cases**: Enables complex document processing for bank statements (account details + transactions), invoices (vendor info + line items), and medical records (patient info + procedures)

- **Enhanced Documentation and Examples**
  - New example notebooks with improved clarity, modularity, and documentation

- **Evaluation Framework Enhancements**
  - Added confidence threshold to evaluation outputs to enable prioritizing accuracy results for attributes with higher confidence thresholds

- **Comprehensive Metering Data Collection**
  - The system now captures and stores detailed metering data for analytics, including:
    - Which services were used (Textract, Bedrock, etc.)
    - What operations were performed (analyze_document, Claude, etc.)
    - How many resources were consumed (pages, tokens, etc.)

- **Reporting Database Documentation**
  - Added comprehensive reporting database documentation

### Changed

- Pin packages to tested versions to avoid vulnerability from incompatible new package versions.
- Updated reporting data to use document's queued_time for consistent timestamps
- Create new extensible SaveReportingData class in idp_common package for saving evaluation results to Parquet format
- Remove save_to_reporting from evaluation_function and replace with Lambda invocation, for smaller Lambda packages and better modularity.
- Harden publish process and avoid package version bloat by purging previous build artifacts before re-building

### Fixed

- Defend against non-numeric confidence_threshold values in the configuration - avoid float conversion or numeric comparison exceptions in Assessement step
- Prevent creation of empty configuration fields in UI
- Firefox browser issues with signed URLs (PR #14)
- Improved S3 Partition Key Format for Better Date Range Filtering:
  - Updated reporting data partition keys to use YYYY-MM format for month and YYYY-MM-DD format for day
  - Enables easier date range filtering in analytics queries across different months and years
  - Partition structure now: `year=2024/month=2024-03/day=2024-03-15/` instead of `year=2024/month=03/day=15/`

## [0.3.3]

### Added

- **Amazon Nova Model Fine-tuning Support**
  - Added comprehensive `ModelFinetuningService` class for managing Nova model fine-tuning workflows
  - Support for fine-tuning Amazon Nova models (Nova Lite, Nova Pro) using Amazon Bedrock
  - Complete end-to-end workflow including dataset preparation, job creation, provisioned throughput management, and inference
  - CLI tools for fine-tuning workflow:
    - `prepare_nova_finetuning_data.py` - Dataset preparation from RVL-CDIP or custom datasets
    - `create_finetuning_job.py` - Fine-tuning job creation with automatic IAM role setup
    - `create_provisioned_throughput.py` - Provisioned throughput management for fine-tuned models
    - `inference_example.py` - Model inference and evaluation with comparison capabilities
  - CloudFormation integration with new parameters:
    - `CustomClassificationModelARN` - Support for custom fine-tuned classification models in Pattern-2
    - `CustomExtractionModelARN` - Support for custom fine-tuned extraction models in Pattern-2
  - Automatic integration of fine-tuned models in classification and extraction model selection dropdowns
  - Comprehensive documentation in `docs/nova-finetuning.md` with step-by-step instructions
  - Example notebooks:
    - `finetuning_dataset_prep.ipynb` - Interactive dataset preparation
    - `finetuning_model_service_demo.ipynb` - Service usage demonstration
    - `finetuning_model_document_classification_evaluation.ipynb` - Model evaluation
  - Built-in support for Bedrock fine-tuning format with multi-modal capabilities
  - Data splitting and validation set creation
  - Cost optimization features including provisioned throughput deletion
  - Performance metrics and accuracy evaluation tools

- **Assessment Feature for Extraction Confidence Evaluation (EXPERIMENTAL)**
  - Added new assessment service that evaluates extraction confidence using LLMs to analyze extraction results against source documents
  - Multi-modal assessment capability combining text analysis with document images for comprehensive confidence scoring
  - UI integration with explainability_info display showing per-attribute confidence scores, thresholds, and explanations
  - Optional deployment controlled by `IsAssessmentEnabled` parameter (defaults to false)
  - Added e2e-example-with-assessment.ipynb notebook for testing assessment workflow

- **Enhanced Evaluation Framework with Confidence Integration**
  - Added confidence fields to evaluation reports for quality analysis
  - Automatic extraction and display of confidence scores from assessment explainability_info
  - Enhanced JSON and Markdown evaluation reports with confidence columns
  - Backward compatible integration - shows "N/A" when confidence data unavailable

- **Evaluation Analytics Database and Reporting System**
  - Added comprehensive ReportingDatabase (AWS Glue) with structured evaluation metrics storage
  - Three-tier analytics tables: document_evaluations, section_evaluations, and attribute_evaluations
  - Automatic partitioning by date and document for efficient querying with Amazon Athena
  - Detailed metrics tracking including accuracy, precision, recall, F1 score, execution time, and evaluation methods
  - Added evaluation_reporting_analytics.ipynb notebook for comprehensive performance analysis and visualization
  - Multi-level analytics with document, section, and attribute-level insights
  - Visual dashboards showing accuracy distributions, performance trends, and problematic patterns
  - Configurable filters for date ranges, document types, and evaluation thresholds
  - Integration with existing evaluation framework - metrics automatically saved to database
  - ReportingDatabase output added to CloudFormation template for easy reference

### Fixed

- Fixed build failure related to pandas, numpy, and PyMuPDF dependency conflicts in the idp_common_pkg package
- Fixed deployment failure caused by CodeBuild project timeout, by raising TimeoutInMinutes property
- Added missing cached token metrics to CloudWatch dashboards
- Added Bedrock model access prerequisite to README and deployment doc.

## [0.3.2]

### Added

- **Cost Estimator UI Feature for Context Grouping and Subtotals**
  - Added context grouping functionality to organize cost estimates by logical categories (e.g. OCR, Classification, etc.)
  - Implemented subtotal calculations for better cost breakdown visualization

- **DynamoDB Caching for Resilient Classification**
  - Added optional DynamoDB caching to the multimodal page-level classification service to improve efficiency and resilience
  - Cache successful page classification results to avoid redundant processing during retries when some pages fail due to throttling
  - Exception-safe caching preserves successful work even when individual threads or the overall process fails
  - Configurable via `cache_table` parameter or `CLASSIFICATION_CACHE_TABLE` environment variable
  - Cache entries scoped to document ID and workflow execution ARN with automatic TTL cleanup (24 hours)
  - Significant cost reduction and improved retry performance for large multi-page documents

### Fixed

- "Use as Evaluation Baseline" incorrectly sets document status back to QUEUED. It should remain as COMPLETED.

## [0.3.1]

### Added

- **{DOCUMENT_IMAGE} Placeholder Support in Pattern-2**
  - Added new `{DOCUMENT_IMAGE}` placeholder for precise image positioning in classification and extraction prompts
  - Enables strategic placement of document images within prompt templates for enhanced multimodal understanding
  - Supports both single images and multi-page documents (up to 20 images per Bedrock constraints)
  - Full backward compatibility - existing prompts without placeholder continue to work unchanged
  - Seamless integration with existing `{FEW_SHOT_EXAMPLES}` functionality
  - Added warning logging when image limits are exceeded to help with debugging
  - Enhanced documentation across classification.md, extraction.md, few-shot-examples.md, and pattern-2.md

### Fixed

- When encountering excessive Bedrock throttling, service returned 'unclassified' instead of retrying, when using multi-modal page level classification method.
- Minor documentation issues.

## [0.3.0]

### Added

- **Visual Edit Feature for Document Processing**
  - Interactive visual interface for editing extracted document data combining document image display with overlay annotations and form-based editing.
  - Split-Pane Layout, showing page image(s) and extraction inference results side by side
  - Zoom & Pan Controls for page image
  - Bounding Box Overlay System (Pattern-1 BDA only)
  - Confidence Scores (Pattern-1 BDA only)
  - **User Experience Benefits**
    - Visual context showing exactly where data was extracted from in original documents
    - Precision editing with visual verification ensuring accuracy of extracted data
    - Real-time visual connection between form fields and document locations
    - Efficient workflow eliminating context switching between viewing and editing

- **Enhanced Few Shot Example Support in Pattern-2**
  - Added comprehensive few shot learning capabilities to improve classification and extraction accuracy
  - Support for example-based prompting with concrete document examples and expected outputs
  - Configuration of few shot examples through document class definitions with `examples` field
  - Each example includes `name`, `classPrompt`, `attributesPrompt`, and `imagePath` parameters
  - **Enhanced imagePath Support**: Now supports single files, local directories, or S3 prefixes with multiple images
    - Automatic discovery of all image files with supported extensions (`.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.tiff`, `.tif`, `.webp`)
    - Images sorted alphabetically in prompt by filename for consistent ordering
  - Automatic integration of examples into classification and extraction prompts via `{FEW_SHOT_EXAMPLES}` placeholder
  - Demonstrated in `config_library/pattern-2/few_shot_example` configuration with letter, email, and multi-page bank-statement examples
  - Environment variable support for path resolution (`CONFIGURATION_BUCKET` and `ROOT_DIR`)
  - Updated documentation in classification and extraction README files and Pattern-2 few-shot examples guide

- **Bedrock Prompt Caching Support**
  - Added support for `<<CACHEPOINT>>` delimiter in prompts to enable Bedrock prompt caching
  - Prompts can now be split into static (cacheable) and dynamic sections for improved performance and cost optimization
  - Available in classification, extraction, and summarization prompts across all patterns
  - Automatic detection and processing of cache point delimiters in BedrockClient

- **Configuration Library Support**
  - Added `config_library/` directory with pre-built configuration templates for all patterns
  - Configuration now loaded from S3 URIs instead of being defined inline in CloudFormation templates
  - Support for multiple configuration presets per pattern (e.g., default, checkboxed_attributes_extraction, medical_records_summarization, few_shot_example)
  - New `ConfigurationDefaultS3Uri` parameter allows specifying custom S3 configuration sources
  - Enhanced configuration management with separation of infrastructure and business logic

### Fixed

- **Lambda Configuration Reload Issue**
  - Fixed lambda functions loading configuration globally which prevented configuration updates from being picked up during warm starts

### Changed

- **Simplified Model Configuration Architecture**
  - Removed individual model parameters from main template: `Pattern1SummarizationModel`, `Pattern2ClassificationModel`, `Pattern2ExtractionModel`, `Pattern2SummarizationModel`, `Pattern3ExtractionModel`, `Pattern3SummarizationModel`, `EvaluationLLMModelId`
  - Model selection now handled through enum constraints in UpdateSchemaConfig sections within each pattern template
  - Added centralized `IsSummarizationEnabled` parameter (true|false) to control summarization functionality across all patterns
  - Updated all pattern templates to use new boolean parameter instead of checking if model is "DISABLED"
  - Refactored IsSummarizationEnabled conditions in all pattern templates to use the new parameter
  - Maintained backward compatibility while significantly reducing parameter complexity

- **Documentation Restructure**
  - Simplified and condensed README
  - Added new ./docs folder with detailed documentation
  - New Contribution Guidelines
  - GitHub Issue Templates
  - Added documentation clarifying the separation between GenAIIDP solution issues and underlying AWS service concerns

## [0.2.20]

### Added

- Added document summarization functionality
  - New summarization service with default model set to Claude 3 Haiku
  - New summarization function added to all patterns
  - Added end-to-end document summarization notebook example
- Added Bedrock Guardrail integration
  - New parameters BedrockGuardrailId and BedrockGuardrailVersion for optional guardrail configuration
  - Support for applying guardrails in Bedrock model invocations (except classification)
  - Added guardrail functionality to Knowledge Base queries
  - Enhanced security and content safety for model interactions
- Improved performance with parallelized operations
  - Enhanced EvaluationService with multi-threaded processing for faster evaluation
    - Parallel processing of document sections using ThreadPoolExecutor
    - Intelligent attribute evaluation parallelization with LLM-specific optimizations
    - Dynamic batch sizing based on workload for optimal resource utilization
  - Reimplemented Copy to Baseline functionality with asynchronous processing
    - Asynchronous Lambda invocation pattern for processing large document collections
    - EvaluationStatus-based progress tracking and UI integration
    - Batch-based S3 object copying for improved efficiency
    - File operation batching with optimal batch size calculation
- Fine-grained document status tracking for UI real-time progress updates
  - Added status transitions (QUEUED → STARTED → RUNNING → OCR → CLASSIFYING → EXTRACTING → POSTPROCESSING → SUMMARIZING → COMPLETE)
- Default OCR configuration now includes LAYOUT, TABLES, SIGNATURE, and markdown generation now supports tables (via textractor[pandas])
- Added document reprocessing capability to the UI - New "Reprocess" button with confirmation dialog

### Changed

- Refactored code for better maintainability
- Updated UI components to support markdown table viewing
- Set default evaluation model to Claude 3 Haiku
- Improved AppSync timeout handling for long-running file copy operations
- Added security headers to UI application per security requirements
- Disabled GraphQL introspection for AppSync API to enhance security
- Added LogLevel parameter to main stack (default WARN level)
- Integration of AppSync helper package into idp_common_pkg
- Various bug fixes and improvements
- Enhanced the Hungarian evaluation method with configurable comparators
- Added dynamic UI form fields based on evaluation method selection
- Fixed multi-page standard output BDA processing in Pattern 1

## [0.2.19]

- Added enhanced EvaluationService with smart attribute discovery and evaluation
  - Automatically discovers and evaluates attributes not defined in configuration
  - Applies default semantic evaluation to unconfigured attributes using LLM method
  - Handles all attribute cases: in both expected/actual, only in expected, only in actual
  - Added new demo notebook examples showing smart attribute discovery in action
- Added SEMANTIC evaluation method using embedding-based comparison

## [0.2.18]

- Improved error handling in service classes
- Support for enum config schema and corresponding picklist in UI. Used for Textract feature selection.
- Removed LLM model choices preserving only multi-modal modals that support multiple image attachments
- Added support for textbased holistic packet classification in Pattern 2
- New holistic classification method in ClassifierService for multi-document packet processing
- Added new example notebook "e2e-holistic-packet-classification.ipynb" demonstrating the holistic classification approach
- Updated Pattern 2 template with parameter for ClassificationMethod selection (multimodalPageLevelClassification or textbasedHolisticClassification)
- Enhanced documentation and READMEs with information about classification methods
- Reorganized main README.md structure for improved navigation and readability

## [0.2.17]

### Enhanced Textract OCR Features

- Added support for Textract advanced features (TABLES, FORMS, SIGNATURES, LAYOUT)
- OCR results now output in rich markdown format for better visualization
- Configurable OCR feature selection through schema configuration
- Improved metering and tracking for different Textract feature combinations

## [0.2.16]

### Add additional model choice

- Claude, Nova, Meta, and DeepSeek model selection now available

### New Document-Based Architecture

The `idp_common_pkg` introduces a unified Document model approach for consistent document processing:

#### Core Classes

- **Document**: Central data model that tracks document state through the entire processing pipeline
- **Page**: Represents individual document pages with OCR results and classification
- **Section**: Represents logical document sections with classification and extraction results

#### Service Classes

- **OcrService**: Processes documents with AWS Textract or Amazon Bedrock and updates the Document with OCR results
- **ClassificationService**: Classifies document pages/sections using Bedrock or SageMaker backends
- **ExtractionService**: Extracts structured information from document sections using Bedrock

### Pattern Implementation Updates

- Lambda functions refactored, and significantly simplified, to use Document and Section objects, and new Service classes

### Key Benefits

1. **Simplified Integration**: Consistent interfaces make service integration straightforward
2. **Improved Maintainability**: Unified data model reduces code duplication and complexity
3. **Better Error Handling**: Standardized approach to error capture and reporting
4. **Enhanced Traceability**: Complete document history throughout the processing pipeline
5. **Flexible Backend Support**: Easy switching between Bedrock and SageMaker backends
6. **Optimized Resource Usage**: Focused document processing for better performance
7. **Granular Package Installation**: Install only required components with extras syntax

### Example Notebook

A new comprehensive Jupyter notebook demonstrates the Document-based workflow:

- Shows complete end-to-end processing (OCR → Classification → Extraction)
- Uses AWS services (S3, Textract, Bedrock)
- Demonstrates Document object creation and manipulation
- Showcases how to access and utilize extraction results
- Provides a template for custom implementations
- Includes granular package installation examples (`pip install "idp_common_pkg[ocr,classification,extraction]"`)

This refactoring sets the foundation for more maintainable, extensible document processing workflows with clearer data flow and easier troubleshooting.

### Refactored publish.sh script

- improved modularity with functions
- improved checksum logic to determine when to rebuild components