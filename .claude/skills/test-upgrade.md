# Test Stack Upgrade (version X → Y)

Use this to validate an **in-place CloudFormation stack upgrade** between two
published GenAI-IDP releases — e.g. "does upgrading a 0.5.16 stack to 0.6.1
succeed, or does it fail and roll back?" This reproduces exactly what a
customer does with the AWS console **Update stack** utility against the public
`idp-main_<version>.yaml` template. It is the go-to when a user reports an
upgrade/rollback failure (like the `PATTERNSTACK` / `UpdateDefaultConfig`
deadlock).

Complementary to `live-eval-and-cost.md` (accuracy/cost A-B) and
`full-test-battery.md` (test suites). This skill is purely about **"does the
CFN stack update apply cleanly, without rollback"**.

> All AWS calls use `AWS_PROFILE=default`. **Confirm the account first** —
> `AWS_PROFILE=default aws sts get-caller-identity` — the token expires often;
> if it returns `ExpiredToken`, ask the user to refresh (`! aws sso login
> --profile default`) before proceeding. Deploy target for this repo's test
> work: region **us-west-2**, in whichever account `AWS_PROFILE=default`
> resolves to (the expected account id is in the `idpagentic-deploy-target`
> memory, not in this file).
>
> **Stale AWS credential env vars shadow the profile.** `AWS_PROFILE=default`
> on the command line does NOT override `AWS_ACCESS_KEY_ID` /
> `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` if those are exported in the
> shell — env-var creds always win, so an expired set produces `ExpiredToken`
> even after the profile token is refreshed. If `sts get-caller-identity`
> fails, check for and unset them BEFORE asking the user to re-auth:
> ```bash
> env | grep -E '^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN|SECURITY_TOKEN|CREDENTIAL_EXPIRATION)='
> unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN AWS_CREDENTIAL_EXPIRATION
> ```
> Note: `unset` does not persist across separate Bash tool calls (each call is
> a fresh shell sourced from the profile). If the profile itself exports these,
> prefix the failing command with the unset, or fix the shell profile. A
> harmless leftover `AWS_PROFILE`/`AWS_REGION` in the env is fine — only the
> raw key/token vars shadow the profile.

---

## 0. Inputs you need

- **FROM version** (base to deploy fresh), e.g. `0.5.16`
- **TO version** (upgrade target), e.g. `0.6.1`
- A **throwaway stack name**, e.g. `UpgradeTest0517to061`
- **Region** (default `us-west-2`)

## 1. Get the template URLs (from CHANGELOG.md)

Every release in `CHANGELOG.md` lists its template URLs. Grep them — don't
hand-type:
```bash
grep -nE "idp-main_<VERSION>\.yaml" CHANGELOG.md
```
Canonical us-west-2 pattern:
```
https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_<VERSION>.yaml
```
Also available in `us-east-1` and `eu-central-1` (swap the region in both the
host and the `aws-ml-blog-<region>` bucket).

> These are the **published** artifacts. To instead test an upgrade to
> **local code**, build with `python3 publish.py <bucket-base> <prefix>
> <region>` and use the resulting `idp-main.yaml` S3 URL as the TO template.

## 2. Inspect required parameters before deploying

The template is a public object — download and list which parameters have **no
default** (those you must supply):
```bash
cd /tmp && curl -s -o idp-from.yaml "<FROM_URL>"
# Required params = those with a Type but no Default. For recent releases the
# ONLY required one is AdminEmail; everything else defaults.
```
Confirm this per release (parameters drift between versions). A quick check:
```bash
python3 - <<'EOF'
import re
txt=open('/tmp/idp-from.yaml').read().splitlines()
inp=False;cur=None;p={}
for ln in txt:
    if ln.rstrip()=='Parameters:': inp=True; continue
    if inp and re.match(r'^[A-Za-z]',ln): break
    if inp:
        m=re.match(r'^  ([A-Za-z0-9]+):\s*$',ln)
        if m: cur=m.group(1);p[cur]={};continue
        if cur:
            mm=re.match(r'^    (Default|Type):\s*(.*)$',ln)
            if mm: p[cur][mm.group(1)]=mm.group(2).strip()
print("REQUIRED:", [k for k,v in p.items() if 'Default' not in v])
EOF
```

## 3. Deploy the FROM base stack

```bash
STACK=UpgradeTest0517to061 ; REGION=us-west-2
AWS_PROFILE=default aws cloudformation create-stack \
  --stack-name "$STACK" --region "$REGION" \
  --template-url "<FROM_URL>" \
  --parameters ParameterKey=AdminEmail,ParameterValue=<your.email@example.com> \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
  --disable-rollback   # keep failed resources inspectable if CREATE fails

AWS_PROFILE=default aws cloudformation wait stack-create-complete \
  --stack-name "$STACK" --region "$REGION"
```
`CAPABILITY_AUTO_EXPAND` is **required** (nested stacks + SAM transform).
Poll status while waiting:
```bash
AWS_PROFILE=default aws cloudformation describe-stacks --stack-name "$STACK" \
  --region "$REGION" --query 'Stacks[0].StackStatus' --output text
```
Base create takes ~20-40 min (CodeBuild builds the UI, containers push to ECR).

## 4. Smoke-test the FROM stack (baseline) — REQUIRED

**Always process `samples/lending_package.pdf` on the FROM stack before
upgrading**, and capture the output as a baseline. This proves the stack is
functional pre-upgrade and gives you a diff target to confirm the upgrade
preserved basic functionality (an upgrade that reaches `UPDATE_COMPLETE` but
silently breaks extraction is still a regression).

```bash
STACK=UpgradeTest0517to061 ; REGION=us-west-2
idp-cli run-inference --stack-name "$STACK" --region "$REGION" \
  --profile default --dir samples/ --file-pattern 'lending_package.pdf' \
  --batch-prefix pre-upgrade --monitor
# note the batch-id it prints, then pull the results:
idp-cli download-results --stack-name "$STACK" --region "$REGION" \
  --profile default --batch-id <batch-id> --output-dir /tmp/upgrade-baseline
```
> `idp-cli` may import a STALE `idp_common` from another checkout — this only
> corrupts `config-download`/`config-upload` (silently strips v0.6 fields), NOT
> `run-inference`/`download-results`. So doc processing is safe, but do not
> round-trip configs through the CLI here. (See `idpagentic-deploy-target`.)

Keep `/tmp/upgrade-baseline` — §8 compares the post-upgrade run against it.

## 5. Upgrade to the TO version (the actual test)

Use the console-equivalent `update-stack`, reusing all existing parameter
values so the diff is purely the template:
```bash
AWS_PROFILE=default aws cloudformation update-stack \
  --stack-name "$STACK" --region "$REGION" \
  --template-url "<TO_URL>" \
  --parameters ParameterKey=AdminEmail,UsePreviousValue=true \
              $(: reuse EVERY other param) \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND
```
> To reuse **all** previous parameter values without listing them, first read
> them and emit `UsePreviousValue=true` for each:
> ```bash
> AWS_PROFILE=default aws cloudformation describe-stacks --stack-name "$STACK" \
>   --region "$REGION" --query 'Stacks[0].Parameters[].ParameterKey' --output text \
>   | tr '\t' '\n' | sed 's/.*/ParameterKey=&,UsePreviousValue=true/'
> ```
> Paste those into `--parameters`. (A parameter that no longer exists in the TO
> template must be dropped; a new required param in TO must be given a value.)

Then wait and watch:
```bash
AWS_PROFILE=default aws cloudformation wait stack-update-complete \
  --stack-name "$STACK" --region "$REGION"   # returns non-zero on rollback
```

## 6. Monitor the config custom resource during the update

The highest-risk step in an X→Y upgrade is the `UpdateDefaultConfig` custom
resource in the nested **PATTERNSTACK** (`patterns/unified/template.yaml`). It
re-validates `config_library/pricing.yaml`, `model_config_limits.yaml`, and
runs the v0.5→v0.6 config migration **on both update AND rollback** — a
validation failure there deadlocks the nested stack in
`UPDATE_ROLLBACK_FAILED`. Tail its Lambda while the update runs:
```bash
FN=$(AWS_PROFILE=default aws lambda list-functions --region "$REGION" \
  --query "Functions[?starts_with(FunctionName,'${STACK}') && contains(FunctionName,'UpdateConfiguration')].FunctionName" \
  --output text)
AWS_PROFILE=default aws logs tail "/aws/lambda/$FN" --since 15m --follow --region "$REGION" \
  | grep -iE "error|units|valid|migrat|traceback|pydantic"
```

## 7. Diagnose a failure

If the update or rollback fails, the parent's failed-resource list is mostly
**collateral** — find the true root cause in the nested stack's own events:
```bash
# Parent failures (includes collateral siblings)
AWS_PROFILE=default aws cloudformation describe-stack-events --stack-name "$STACK" \
  --region "$REGION" \
  --query "StackEvents[?contains(ResourceStatus,'FAILED')].[Timestamp,LogicalResourceId,ResourceStatusReason]" \
  --output table | head -40

# Drill into the nested PATTERNSTACK (usual culprit)
PS=$(AWS_PROFILE=default aws cloudformation describe-stack-resources --stack-name "$STACK" \
  --region "$REGION" \
  --query "StackResources[?LogicalResourceId=='PATTERNSTACK'].PhysicalResourceId" --output text)
AWS_PROFILE=default aws cloudformation describe-stack-events --stack-name "$PS" \
  --region "$REGION" \
  --query "StackEvents[?contains(ResourceStatus,'FAILED')].[LogicalResourceId,ResourceStatusReason]" \
  --output table | head -40
```

### Recovery from `UPDATE_ROLLBACK_FAILED` (pricing/config deadlock)
The custom resource reads `pricing.yaml` from the **ConfigurationBucket S3
path**, not the template. Fix the S3 object, then continue the rollback:
```bash
# validate a candidate pricing.yaml locally first
PYTHONPATH=lib/idp_common_pkg python3 -c "import yaml; from idp_common.config.models import PricingConfig; PricingConfig(**yaml.safe_load(open('config_library/pricing.yaml')))"
# upload corrected file to the config bucket key config_library/pricing.yaml, then:
AWS_PROFILE=default aws cloudformation continue-update-rollback --stack-name "$STACK" --region "$REGION"
# NOTE: no --resources-to-skip; child stacks reject direct skip. Fixing S3 lets
# the parent rollback complete once re-validation passes.
```
(See the `pricing-units-rollback-deadlock` memory for the verified recovery.)

## 8. Confirm success

```bash
AWS_PROFILE=default aws cloudformation describe-stacks --stack-name "$STACK" \
  --region "$REGION" --query 'Stacks[0].[StackStatus]' --output text
# want: UPDATE_COMPLETE  (NOT UPDATE_ROLLBACK_COMPLETE / _FAILED)
```
Optionally confirm the version bumped:
```bash
AWS_PROFILE=default aws cloudformation describe-stacks --stack-name "$STACK" \
  --region "$REGION" --query "Stacks[0].Outputs[?contains(OutputKey,'ersion')]" --output table
```

## 9. Smoke-test the upgraded stack & compare to baseline — REQUIRED

Re-run the **same** document on the upgraded stack and diff the output against
the §4 baseline. `UPDATE_COMPLETE` alone is not a pass — functionality must be
preserved.

```bash
idp-cli run-inference --stack-name "$STACK" --region "$REGION" \
  --profile default --dir samples/ --file-pattern 'lending_package.pdf' \
  --batch-prefix post-upgrade --monitor
idp-cli download-results --stack-name "$STACK" --region "$REGION" \
  --profile default --batch-id <batch-id> --output-dir /tmp/upgrade-after
```
Compare the extraction/classification results:
```bash
diff -r /tmp/upgrade-baseline /tmp/upgrade-after
```
Expect the document to process to completion with comparable extracted values.
Some drift is normal and NOT a regression: LLM non-determinism, and any
config-shape changes the v0.5→v0.6 migration intentionally introduces (e.g.
confidence/assessment structure, new metering fields). What you're ruling out
is a **functional break** — empty extractions, a workflow that errors/times
out, classes no longer detected, or confidence collapsing to null across the
board. Call out clearly whether the doc still processes and whether extracted
values are materially equivalent.

## 10. Tear down

Throwaway stacks should be deleted once the result is recorded (unless a
failure needs inspection — keep it and tell the user):
```bash
AWS_PROFILE=default aws cloudformation delete-stack --stack-name "$STACK" --region "$REGION"
AWS_PROFILE=default aws cloudformation wait stack-delete-complete --stack-name "$STACK" --region "$REGION"
```
Buckets with `DeletionPolicy: Retain` (Input/Output/Config) may remain — empty
and delete them manually if doing a full cleanup.

---

## Checklist

1. [ ] Creds valid (`sts get-caller-identity` returns the expected account); no stale
       `AWS_ACCESS_KEY_ID`/`SESSION_TOKEN` env vars shadowing the profile
2. [ ] Template URLs for FROM + TO grepped from CHANGELOG
3. [ ] Required params confirmed from the FROM template
4. [ ] Base FROM stack CREATE_COMPLETE
5. [ ] **Baseline**: `lending_package.pdf` processed on FROM stack, output saved
6. [ ] `update-stack` to TO template, all params reused (drop params removed in TO)
7. [ ] `UpdateConfiguration` Lambda logs watched during update
8. [ ] Final status `UPDATE_COMPLETE` (no rollback)
9. [ ] **Compare**: `lending_package.pdf` re-processed post-upgrade; output vs
       baseline confirms functionality preserved (no functional break)
10. [ ] Result reported; throwaway stack deleted (or kept if failed)
