Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Review Routing Showcase Configuration

This directory contains a configuration that shows review routing: one routing table that decides, for each processed document, whether it is reviewed, in which review queue, and with which reason for the reviewer. Four sample documents from this repository each reach a different queue. Three scenario invoices with known problems return to review once for each problem that remains.

## Processing Mode

**Default Mode**: Pipeline (use_bda: false) — Amazon Textract for OCR, then Bedrock LLM for classification, extraction, confidence scoring and policy rule validation.

## Validation Level

**Level**: 1 - Basic Testing

- **Testing Evidence**: The configuration loads and its routing table passes the same validation a save from the web UI runs. An offline simulation evaluates the table for each scenario document, review round by review round (`lib/idp_common_pkg/tests/unit/config/test_routing_showcase_preset.py`). Where each sample document lands on a deployed stack, and the reason its reviewer reads, is checked by `scripts/review_routing_live_check.py`.
- **Known Limitations**: The classes copied from other presets are not tuned together. Policy rule verdicts come from a model and can differ between runs, so a routing rule that reads one can match in one run of a document and not in another.

## Before you save it

The routing rules name three review queues by id. A configuration whose routing rule names a queue that does not exist is refused when it is saved from the web UI, so create them first, in **Review Studio › Review Management › Create queue** or with the CLI:

```bash
idp-cli review-queues create --stack-name <stack> --id specialist --name Specialist
idp-cli review-queues create --stack-name <stack> --id compliance --name Compliance
idp-cli review-queues create --stack-name <stack> --id approvals --name Approvals
```

Then upload the configuration as a profile:

```bash
idp-cli config-upload --stack-name <stack> \
    --config-file config_library/unified/review-routing-showcase/config.yaml \
    --config-profile review-routing-showcase
```

Routing holds a document only while Review Routing is on in its configuration (`hitl.enabled`, which this preset turns on) and the Default review queue is enabled.

## The routing table

The routing rules are tried in order, after policy rule validation, and the first one whose conditions all hold decides.

| Order | Routing rule | Condition | Queue |
|---|---|---|---|
| 1 | `scenario-po-without-purchase-order` | Not reviewed yet; the `InvoiceType` of a `Scenario-Invoice` section is `PO-backed`; no section is classified `Scenario-Purchase-Order` | `approvals` |
| 2 | `scenario-net-amount-check` | Not reviewed yet; the policy rule `scenario_invoice_checks.net_amount_matches` has the verdict `fail` | `compliance` |
| 3 | `scenario-vendor-not-approved` | The `Vendor` of a `Scenario-Invoice` section is neither `AnyCompany Office Supplies` nor `Example Print Services` | `approvals` |
| 4 | `scenario-payment-terms-missing` | A section is classified `Scenario-Invoice`, and no such section has a value for `PaymentTerms` | `default` |
| 5 | `specialist-bank-checks` | Not reviewed yet; a section of the document is classified `Bank-checks` | `specialist` |
| 6 | `compliance-policy-failure` | Not reviewed yet; at least one policy rule has the verdict `fail` | `compliance` |
| 7 | `approvals-high-value` | Not reviewed yet; the `GrossTotal` of an `Invoice` section is over 1000 | `approvals` |
| 8 | `default-w2-forms` | Not reviewed yet; a section of the document is classified `W2` | `default` |
| 9 | `builtin-extraction-failed` | Not reviewed yet; the extraction of a section failed without failing the document | `default` |
| 10 | `builtin-confidence-alerts` | Not reviewed yet; a field is under its confidence threshold | `default` |
| 11 | `builtin-missing-required` | Not reviewed yet; a required field has no extracted value | `default` |

A document that matches none of the routing rules is not reviewed (the default outcome).

"Not reviewed yet" is the condition `reviewed` is `false`. A routing rule that has it does not send a document whose review was completed to a queue again. `scenario-vendor-not-approved` and `scenario-payment-terms-missing` do not have it: they hold a document again after a review, until the value they test is corrected. `routing.max_consecutive_placements` limits how many times in a row a document is placed.

### The missing-class routing rule

`scenario-po-without-purchase-order` uses the `class_present` input, which is true when a section of the document has the named class. With the test `false`, the routing rule matches a document that lacks a class: here, an invoice that says it is backed by a purchase order, in a document that holds no purchase order. The class has to be one of the profile's classes, which is why this preset defines `Scenario-Purchase-Order` although no sample is one.

## Reasons

Every routing rule has a `reason`: the text the reviewer reads when that routing rule places a document. A placeholder in braces takes the document's value.

| Placeholder | Shows | In this preset |
|---|---|---|
| `{input}` | A routing input | `{section_classes}`, `{failed_policy_rule_count}` |
| `{input:path}` | A routing input that takes a path, such as an extracted field | `{field_value:Scenario-Invoice:Vendor}` |
| `{condition_N.value}` | What the N-th condition of the routing rule compares with | `{condition_1.value}` in `scenario-vendor-not-approved`: the approved vendors |
| `{confidence_threshold}` | The profile's confidence threshold | |

`scenario-vendor-not-approved` has this reason:

```text
Vendor {field_value:Scenario-Invoice:Vendor} is not on the approved vendor list ({condition_1.value}). Enter the vendor's registered name.
```

The reviewer of `routing-scenario-one-problem.pdf` reads:

```text
Vendor AnyCo Office is not on the approved vendor list (AnyCompany Office Supplies, Example Print Services). Enter the vendor's registered name.
```

A reason is checked when the configuration is saved: each placeholder has to name a routing input, a field of a class, a policy rule of the Policy Schema or a condition the routing rule has. The built-in routing rules carry the reasons of the system defaults.

## Which sample goes where

Upload each sample under the `review-routing-showcase` profile.

| Sample | Queue | Why |
|---|---|---|
| `samples/lending_package.pdf` | Specialist | The package contains a bank check |
| `samples/rule-validation/medicare_respiratory_pa_packet.pdf` | Compliance | Policy rule validation finds failed policy rules |
| `samples/fcc-invoices-7429ba503d96718740c2f714598b40a4.pdf` | Approvals | The invoice's gross total is over 1000 |
| A W-2 from `samples/w2/` | Default | The document is a W-2 |

The lending package contains a W-2 as well. It goes to Specialist, because the routing rules are tried in order and the bank check routing rule comes first.

## The scenario documents

`samples/review-routing/` holds three synthetic one-page invoices, written by `scripts/generate_routing_scenario_invoices.py`. Each carries problems that were chosen in advance, and `scenarios.truth.json` beside them records what each page prints. No problem is a confidence score, which differs between runs. Each is a class the document lacks, a value the page prints or leaves out, or a policy rule the printed amounts fail.

| Sample | Problems |
|---|---|
| `samples/review-routing/routing-scenario-one-problem.pdf` | The vendor is printed as `AnyCo Office`, which is not on the approved vendor list |
| `samples/review-routing/routing-scenario-two-problems.pdf` | The net amount due (1,250.00) is not the gross total (1,200.00) minus the discount (100.00); no payment terms are printed |
| `samples/review-routing/routing-scenario-three-problems.pdf` | The invoice is PO-backed and the document holds no purchase order; the vendor is printed as `AnyCo Office`; no payment terms are printed |

Upload each under the `review-routing-showcase` profile and keep its file name. A document whose name contains `routing-scenario` is classified `Scenario-Invoice` by its name, and the policy `scenario_invoice_checks` applies to it.

A document returns to review once for each problem that remains. The first routing rule that matches decides, so one placement shows the reason of one routing rule. When the reviewer completes the review, policy rule validation and Review Routing run again on the corrected data, and the next routing rule that still matches places the document again.

| Document | Round | Routing rule | Queue | The reason names | What the reviewer does |
|---|---|---|---|---|---|
| `routing-scenario-one-problem.pdf` | 1 | `scenario-vendor-not-approved` | `approvals` | `AnyCo Office` | Changes `Vendor` to `AnyCompany Office Supplies` |
| `routing-scenario-one-problem.pdf` | 2 | none | | | Nothing: the document completes |
| `routing-scenario-two-problems.pdf` | 1 | `scenario-net-amount-check` | `compliance` | `1250` | Changes `NetAmountDue` to 1100 |
| `routing-scenario-two-problems.pdf` | 2 | `scenario-payment-terms-missing` | `default` | `INV-2002` | Sets `PaymentTerms` to `Net 30` |
| `routing-scenario-two-problems.pdf` | 3 | none | | | Nothing: the document completes |
| `routing-scenario-three-problems.pdf` | 1 | `scenario-po-without-purchase-order` | `approvals` | `PO-backed` | Checks the purchase order and completes the review with no change |
| `routing-scenario-three-problems.pdf` | 2 | `scenario-vendor-not-approved` | `approvals` | `AnyCo Office` | Changes `Vendor` to `AnyCompany Office Supplies` |
| `routing-scenario-three-problems.pdf` | 3 | `scenario-payment-terms-missing` | `default` | `INV-3003` | Sets `PaymentTerms` to `Net 30` |
| `routing-scenario-three-problems.pdf` | 4 | none | | | Nothing: the document completes |

`scenario-po-without-purchase-order` and `scenario-net-amount-check` apply only before the first review. A reviewer cannot add a purchase order to a document, and policy rule validation reads the page text as well as the extracted values, so a correction does not reliably clear a failed policy rule. Without the "not reviewed yet" condition, those routing rules would hold the document again after every review. The other two scenario routing rules test a value the reviewer can correct, so they hold the document until it is corrected.

The document with three problems is placed three times in a row, which is the value of `routing.max_consecutive_placements` in this preset. A document that still matched a routing rule after that many placements would continue without another review.

## Key Components

### Document classes

Most classes are copied from other presets, unchanged except where stated:

- **Invoice**, from [realkie-fcc-verified](../realkie-fcc-verified/). Its fields carry a confidence threshold of 0, so an invoice raises no confidence alert and is in review only because of its amount.
- **Payslip, US-drivers-licenses, Bank-checks, Bank-Statement, W2, Homeowners-Insurance-Application**, from [lending-package-sample](../lending-package-sample/).
- **PA-Administrative, PA-Medical-History, PA-Operative-Report, PA-Procedure-Log, PA-Claims-Evidence**, with the policy classes, from [rule-validation](../rule-validation/).

The classes of the scenario documents are defined here:

- **Scenario-Invoice**: the class of a document whose file name contains `routing-scenario` (`x-aws-idp-document-name-regex`). Its fields carry a confidence threshold of 0.
- **Scenario-Purchase-Order**: the class the missing-class routing rule tests for. No sample is one.
- The policy class **scenario_invoice_checks**, with the policy rule `net_amount_matches`: the net amount due is the gross total minus the discount. It applies to documents whose file name contains `routing-scenario`.

### Settings

- `hitl.enabled: true`: Review Routing is on.
- `hitl.confidence_threshold: "0.99"`: a field with no threshold of its own raises a confidence alert under this value. It is high so that the samples show confidence alerts among their review reasons. No sample depends on an alert to reach its queue, because a confidence score comes from a model and differs between runs.
- `rule_validation.enabled: true`: policy rule validation runs, and two routing rules read its verdicts.
- `routing`: the table above, with `max_consecutive_placements: 3`.

Everything else is inherited from the system defaults.

## Changing it

Edit the routing rules on the **Routing Rules tab** of the profile in the web UI, where the table can also be tested against a processed document and exported or imported as DMN XML. See [Review routing](../../../docs/review-routing.md).
