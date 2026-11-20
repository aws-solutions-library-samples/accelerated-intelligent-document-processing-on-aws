# Decision Model: Strands Decider (`decision-model-decider`)

A Feature Platform extension that deploys the
[Strands Decider](https://github.com/strands-labs/strands-decider), a decision
model, as the Lambda function `GENAIIDP-decider` for the pipeline's decision-model
client (`lib/idp_common_pkg/idp_common/decision_model/`) to call. User guide:
[docs/extensions/decision-model-decider.md](../../docs/extensions/decision-model-decider.md).

| Path | What it is |
|---|---|
| `template.yaml` | ECR repository, the CodeBuild project and its inline buildspec, the image-build custom resource, the function with its version and `live` alias, and the ui-deployer |
| `image-build/` | Custom resource handler: derives the image tag from the build inputs, starts a build only when ECR lacks that tag, and polls the build (crhelper) |
| `ui-deployer/` | Copies the UI bundle, registers the feature, and installs `config-preset/` with the alias ARN filled in as the section-classification endpoint |
| `config-preset/section-classification.yaml` | The profile the install adds, inactive |
| `feature-ui/` | An information page, built from `feature-platform/sample-feature/feature-ui` |

The image is upstream's `deploy/lambda/Dockerfile`, built unchanged at the commit
in `DeciderSourceRevision`. Both revision parameters accept only full commit SHAs;
the buildspec checks the fetched source against the source commit and asks the Hub
that the model revision is a commit. Changing either default is an extension
release: bump `version` in `feature.yaml`, and check the new model commit's
licence.

Tests: `image-build/tests` and `ui-deployer/tests`, run by `make test` and
`make test-packages-cicd`.

Publish and deploy with the SDK CLI, as for any extension:

```bash
idp-feature-cli validate .
idp-feature-cli deploy --from-code . --host-stack-name <host stack> --wait
```
