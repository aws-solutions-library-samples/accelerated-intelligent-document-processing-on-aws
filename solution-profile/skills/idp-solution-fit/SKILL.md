---
name: idp-solution-fit
description: Assess whether the GenAI IDP Accelerator fits a document-processing need, using its solution profile. Use when someone describes documents they need to classify, extract data from, validate or review, and wants to know whether this accelerator fits, what to ask, and what would have to be built around it.
---

# Assess fit for the GenAI IDP Accelerator

This skill works from the accelerator's **solution profile**, a set of YAML files
published with each release. The profile files sit two directories above this file:
this skill is `skills/idp-solution-fit/SKILL.md` inside the profile directory, beside
`solution-profile.yaml`.

Read the profile of the release you are assessing, not a newer or older one.
`solution-profile.yaml` says which release it describes (`version`) and which release
a person last reviewed it against (`reviewed_for_version`).

## 1. Read the profile

| File | Use it for |
|---|---|
| `solution-profile.yaml` | What the accelerator is, its support model, and `signals`: keywords and phrases that suggest a need it serves, and `anti_signals` that suggest it does not |
| `capabilities.yaml` | What it does, the processing mode each capability works in (`pipeline` or `bda`), and its status (`ga`, `preview`, `experimental`, `deprecated` or `retired`) |
| `limitations.yaml` | Where it stops, with numbers, how much each limit matters (`impact`), the questions that detect it (`detect_with`) and what mitigates it |
| `extension-points.yaml` | Every documented way to change its behaviour, with a gap category, effort and upgrade risk |
| `discovery-questions.yaml` | The questions that decide fit and configuration, and what each answer implies |
| `deployment.yaml` | How and where it deploys, and the prerequisites |
| `sizing.yaml` | The inputs that size a deployment and the accelerator's own estimators |
| `delivery.yaml` | How a delivery usually starts, and typical work items with effort |
| `changes.yaml` | What each release changed, and what an upgrade requires someone to act on |

Capabilities, extension points, limitations and deployment options carry
`evidence`: a path in the accelerator's repository at the release commit, or a
documentation URL. Discovery questions cite their sources in their `implications`. Cite evidence when you state a fact, and
prefer the evidence over your own memory of the product.

Items are cited as `<kind>:<id>`, for example `limitation:simple-extraction-row-limit`.
Use those citations in your notes so a person can check each one.

## 2. Recognise the need

Compare what you are told with `signals` and `anti_signals` in the manifest. A need
is a likely match when documents arrive as files (PDFs, scans, images, Office or text
files) and someone has to classify them, pull out fields or tables, check them
against rules, or review them. It is likely not a match when the core need is
storing and managing documents, authoring or signing them, or processing audio or
video. If you are unsure, say so and ask; do not stretch the match.

## 3. Ask the questions

Work through `discovery-questions.yaml` by `priority` (`must`, then `should`, then
`could`) and by `phase`. `pre-contract` questions decide fit; `discovery` and
`build` questions shape the configuration. Ask each question as written: they are
phrased to be asked directly. Record each answer with the question id.

For each answer, read the question's `implications` and follow its `tests` to the
capabilities and limitations it bears on. A limitation whose `detect_with` names the
question is the one to check first. Follow `follow_ups` when an answer opens one.

Do not assume an answer you were not given. Where an answer is missing, list the
question as open rather than guessing.

## 4. Classify each gap

For every requirement the accelerator does not meet as it stands, choose the **most
preferred** category that closes it. The order of preference is:

1. **configuration**: settings, configuration profiles and versions, presets,
   prompts and JSON Schema classes. No code. The deployment's operator owns it.
2. **hook**: customer-owned code at a documented hook point that the accelerator
   calls. The customer owns it.
3. **extension**: an installable add-on on the accelerator's extension framework,
   deployed beside it. The customer or a partner owns it.
4. **core-enhancement**: a change to the accelerator itself, contributed upstream.
   It ships when the maintainers release it, so its timing is not the customer's to
   decide.
5. **fork**: a private copy with code changes. Least preferred: every upgrade then
   has to be merged by hand, and `changes.yaml` shows how often releases arrive.
6. **other**: work outside the accelerator, with an `other_kind` of `aws-service`,
   `integration`, `data`, `process`, `security-review` or `infrastructure`.

Use the extension points in `extension-points.yaml` as the menu. Name the extension
point you would use, its `effort` and `upgrade_risk`, and its `constraints`, and check
its `modes`: some hook points do not run in BDA mode.

If no category closes a requirement, not even a fork, the verdict for that
requirement is **not-a-fit**. Say which requirement it is and why. One not-a-fit
requirement that is essential makes the accelerator a poor fit overall, however well
the rest matches.

## 5. Report

Give a short assessment a person can check:

- the release the profile describes;
- the match, in one or two sentences, citing capabilities;
- each limitation that applies, with its numbers and the answer that triggered it;
- each gap, with its category, the extension point, effort and upgrade risk;
- any not-a-fit requirement;
- the open questions;
- the support model: the customer deploys, owns and operates the accelerator.

State what you inferred separately from what the profile says, never present a
`preview` or `experimental` capability as generally available, and never recommend a
`deprecated` or `retired` capability for new work.
