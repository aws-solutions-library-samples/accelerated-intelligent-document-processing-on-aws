---
title: "Decision Model: Strands Decider"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Decision Model: Strands Decider

The **Decision Model: Strands Decider** extension is an installable
[Feature Platform](../feature-platform.md) extension that deploys a
[decision model](../decision-models.md) in your account for the pipeline to call.
A decision model picks between options and returns a probability for each one, so
section classification and extraction confidence can use it in place of an LLM.

Installing it:

1. builds the [Strands Decider](https://github.com/strands-labs/strands-decider)'s
   AWS Lambda image in your account, with CodeBuild, from a pinned source commit and
   a pinned model commit;
2. deploys the image as the Lambda function `GENAIIDP-decider`, with a `live` alias;
3. adds a configuration profile named `decision-model-decider` that selects the
   function for section classification.

A stack without the extension installed is unchanged, and installing it does not
change the active configuration: the new profile is inactive until an admin
activates it.

## Install

The extension is listed in the **Extensions** catalog (**Browse catalog**). Choose
**Install** and create the stack the console opens. The image build takes most of
the install: CodeBuild downloads the model and its base model, folds the model's
adapter into the base model, converts and quantizes it, and builds the inference
library. In one install in us-east-1 in November 2026, the build took about 6
minutes with the vision tower, and the whole stack about 10.

From a checkout, the same install is:

```bash
idp-feature-cli deploy --from-code ./feature-platform/decision-model-decider \
    --host-stack-name <your IDP stack> --wait
```

The CodeBuild project reaches `github.com`, `huggingface.co`, `public.ecr.aws`,
PyPI and the PyTorch package index during the build, because the upstream
`deploy/lambda/Dockerfile` installs its pinned Python packages from them. The
function itself makes no network calls: the model is inside its image.

## Use it

Open **Configuration**, select the `decision-model-decider` profile, and review it.
It is your default configuration with section classification changed:

```yaml
classification:
  strategy: split_then_classify
  split_then_classify:
    section_classification:
      method: decision_model
      pages: first
      decision_model:
        endpoint: arn:<partition>:lambda:<region>:<account>:function:GENAIIDP-decider:live
        input: images
        max_pages: 1
```

Activate the profile, or copy these settings into the profile you use. To use the
same function for extraction confidence, set
`extraction.confidence.method: decision_model` and its
`decision_model.endpoint` to the same ARN, which is the extension stack's
`DeciderEndpoint` output. Every setting is described in
[Decision Models](../decision-models.md).

The pipeline needs no new permission: its functions may already invoke any function
named `GENAIIDP-*`, alias included.

## What it builds, and from where

| Input | Default | Parameter |
|---|---|---|
| Upstream source | `strands-labs/strands-decider` at commit `79bf6e7184c166990eec79034667cc0bfd445e12`, the latest commit to its `deploy/lambda` recipe when this extension was written, which added images on Lambda | `DeciderSourceRevision` |
| Model | `StrandsAgents/strands-decider-2B-qwen3.5-v1-2610` on the Hugging Face Hub | `ModelId` |
| Model revision | Hub commit `4d27d8d7edcfd6616806c8684e20e2673684e283`. The model has no dated tags on the Hub, so it is pinned by commit | `ModelRevision` |
| Base model | `Qwen/Qwen3.5-2B-Base` at the revision the pinned model commit records in its `provenance.json` (`b1485b2fa6dfa1287294f269f5fb618e03d52d7c`) | follows `ModelRevision` |
| Vision tower | included, so requests may carry page images | `EnableVision` |

Both revisions accept only a full 40-character commit SHA, and the build checks
each one before it uses it:

- It fetches the source repository's branches and tags and fails unless one of
  them contains the commit, and then unless `git rev-parse HEAD` is that commit
  after checkout. GitHub serves a fork's commits through the parent repository's
  URL too, which is why containing it is required, not merely fetching it.
- It asks the Hub what the model revision resolves to and fails unless the answer
  is the revision itself. A branch name such as `main` resolves to a different
  string, its current commit, so a branch cannot be passed off as a pin.

The image is tagged with a hash of these inputs. A stack update that changes none
of them reuses the image in ECR without building, and a rollback after a change
finds the previous image there. The repository keeps the last three images.

## Licence

The weights are downloaded from the Hugging Face Hub by the build in **your**
account. This project does not redistribute them. At the pinned model commit:

- The model (a LoRA adapter and a readout head) is released under the **Apache
  License 2.0**, according to its Hub licence tag and its `LICENSE.md`.
- Its base model, `Qwen/Qwen3.5-2B-Base`, is released under the **Apache License
  2.0**, according to its Hub licence tag. The image holds the base model's weights
  with the adapter folded in, and its vision tower.
- The model's `LICENSE.md` also describes its training data: public datasets under
  a range of licences, some of them ShareAlike and some with custom or unstated
  terms, listed source by source in the model repository's `training/sources.md`.
  Review those terms against your own use.
- The upstream source the image is built from, `strands-labs/strands-decider`, is
  released under the Apache License 2.0.

If you change `ModelId` or `ModelRevision`, the licence is whatever that model's
repository states at that commit: check it before you install.

## Cost

The figures in the list below are the upstream recipe's own us-east-1
measurements and list prices, from its `deploy/lambda/README.md`; the paragraph
after it is what was measured on this extension.

- **Per call.** The function runs at 10,240 MB on arm64 and is billed by duration.
  Upstream measured a short request at about 1.6 to 2.4 seconds and a 4,000-token
  one at 29 to 45 seconds. Section classification makes one call per section.
- **At rest, SnapStart off (the default).** The image in ECR, about $0.25 a month.
  A new execution environment loads the model before it answers its first request.
  With the vision tower, which this extension includes by default, upstream
  measured 16 to 34 seconds for that load, and 3 minutes for the first environment
  after a deploy; without it, up to about 30 seconds for the first request. The configuration's `timeout_ms` default, 300,000, allows for that.
- **At rest, SnapStart on.** About $39.50 a month for the published version's
  snapshot, whether or not anything calls it, and a new execution environment
  answers in seconds. Each install or update that changes the image, SnapStart, tracing or
  the extension's release publishes a new version and deletes the one it replaces,
  so only one snapshot is billed.
- **Install.** One build on CodeBuild's large ARM machine.

Measured on this extension, in the same us-east-1 install, with one page image and
the six classes of the default configuration: the first request to a new execution
environment took 43.6 seconds end to end, of which loading the model was 31.9, and
the next request to the same environment took 7.8 seconds. The function ran on
Graviton2 both times.

`config_library/pricing.yaml` prices `decision_model/GENAIIDP-decider`, so calls
appear in the cost report under that name. Under another `DeciderFunctionName`, add
an entry for it.

## Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `DeciderFunctionName` | `GENAIIDP-decider` | The function's name. It must start with `GENAIIDP-`. A second host stack in the same account and Region needs another name, because function names are unique there. |
| `EnableSnapStart` | `false` | SnapStart on the published version. See [Cost](#cost). |
| `ReservedConcurrency` | `0` | Reserved concurrency for the function; `0` reserves none. Each concurrent call is a 10,240 MB execution environment drawn from the account's Lambda concurrency, shared with the rest of the pipeline. To cap the decider's share, set it to at most the host's `MaxConcurrentWorkflows` times the configuration's `decision_model.max_concurrency`. A reservation the account cannot spare fails the install, which is why none is the default. |
| `EnableVision` | `true` | Include the vision tower. Without it, a request with images is refused, and section classification and extraction confidence both default to `input: images`. |
| `DeciderSourceRevision`, `ModelId`, `ModelRevision` | the pins above | What the image is built from. |
| `AlarmEmail` | empty | An email address for two alarms on the function, on errors and on throttles. When set, the stack creates the alarms, an SNS topic encrypted with the host's key, and an email subscription, which delivers nothing until the recipient confirms it. Empty creates none of them. A throttle means the account's Lambda concurrency, or `ReservedConcurrency`, could not hold another 10,240 MB execution environment. |
| `EnableXRayTracing` | `true` | X-Ray tracing for this stack's functions. An extension stack does not receive the host's setting. |
| `LogRetentionDays` | `30` | Retention for the function's, the build's and the custom resources' log groups. |

## Logs

The function logs to `/<extension stack>/lambda/DeciderFunction`, one line per
request with its latency, its token count and the host CPU. The image build logs to
`/<extension stack>/codebuild/DeciderImageBuildProject`; a failed install names the
build whose log holds the cause.

## Regions

The install needs CodeBuild's `BUILD_GENERAL1_LARGE` ARM environment and Lambda
container images on arm64 in the stack's Region. Neither, nor SnapStart for a
container-image function, has been checked in the AWS GovCloud (US) Regions; leave
SnapStart off there unless you have confirmed it.

## Uninstall

Delete the extension stack. That removes the function and its versions, the ECR
repository with its images, the CodeBuild project, the log groups and the
configuration profile. If the profile is active, the host activates `default`
first. Before uninstalling, point any other profile that names the function's ARN
elsewhere: a profile that still selects the decision model fails each section it
classifies.
