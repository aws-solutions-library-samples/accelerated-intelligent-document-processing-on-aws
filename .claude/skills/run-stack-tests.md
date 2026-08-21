# Skill: Run stack-tests (`stacktest-*`) manually

Use this when the user wants to run one of the **deploy-variant stack-tests** or
other live-stack integration tests **outside** the CI pipeline — e.g. "run the
ZAP scan against my stack", "test the Jobs API deploys", "check WAF hosting",
"run the private API hosting test", "verify the permissions boundary". These
used to run automatically on every integration pipeline; they were moved to
on-demand `make stacktest-*` targets because standing up ~6 stacks at once in one
pipeline burst the account-wide AWS control planes (CloudWatch Logs
create-consistency, CodeBuild role-trust propagation, IAM CreatePolicy rate
limit) and caused flaky failures unrelated to the code under test.

## The stacktest family

| Target | What it does | Needs |
|---|---|---|
| `make stacktest-list` | List the deploy-variant stack-tests | — |
| `make stacktest-rbac STACK_NAME=…` | Full RBAC/authorization matrix (alias of `api-test`) | live stack |
| `make stacktest-zap` | OWASP ZAP DAST scan of the UI API | stack or self-deploy |
| `make stacktest-hosting-global` | APIGateway GLOBAL hosting variant | stack or self-deploy |
| `make stacktest-waf` | WAF IP-allow-list WebACL association | stack or self-deploy |
| `make stacktest-hosting-private` | PRIVATE (VPC) API hosting | **VPC** + stack/self-deploy |
| `make stacktest-jobsapi` | Jobs REST API (`EnableJobsApi`) | **VPC** + stack/self-deploy |
| `make stacktest-seller` | Seller Entitlement Service e2e (deploys + tears down its own stack) | seller-account creds; **no** listing needed |
| `make stacktest-benchmark` | Release-vs-release benchmark audit (alias) | see run-benchmarks |
| `make stacktest-upgrade` | In-place upgrade test pointer | see test-upgrade |

## Two modes

1. **Validate an existing stack (fast, preferred):**
   `make stacktest-zap STACK_NAME=<already-deployed-stack>` — runs only the
   validator against a stack the user already has. No deploy, no teardown.
2. **Self-deploy a throwaway stack:** omit `STACK_NAME` and pass
   `TEMPLATE_URL=<idp-main.yaml from publish.py>`. Deploys its own stack,
   validates, and tears it down. Slower (~30 min deploy).

Always use `AWS_PROFILE=default` (or `idp-ci`) — see CLAUDE.md.

**Region:** the validators read `AWS_DEFAULT_REGION` (default `us-east-1`), NOT
the active profile's configured region. If your stack is elsewhere, pass
`REGION=` — e.g. `make stacktest-zap STACK_NAME=IDP1 REGION=us-west-2`. Omitting
it when the stack isn't in us-east-1 fails with "stack does not exist".

## Reading the ZAP report

`make stacktest-zap` prints a self-contained report block covering EVERY rule
outcome, not just the findings:

```
🔎 ZAP DAST scan report
  Target:      https://<id>.execute-api.<region>.amazonaws.com/api
  Operations:  96 seeded (POST /op/<field>)
  Mode:        passive baseline        # or "active scan" (IDP_ZAP_ACTIVE=true)
  Rules:       114 PASS · 3 WARN · 0 FAIL · 1 IGNORE
  Alerts:      High=0 Medium=1 Low=3 Info=2
  Findings (most severe first):
    • [Medium] Cross-Domain Misconfiguration — 5 instance(s)
        https://.../op/abortWorkflow
        ↳ fix: <ZAP remediation hint>
  Report:      /path/to/zap-report.html   (+ .json)
```

- **`Rules:`** is the full tally from `zap-api-scan` — `PASS` shows how much was
  actually exercised (a big PASS count is the "nothing found" signal), `FAIL`/`WARN`
  are the actionable ones, `IGNORE` are rules muted in `scripts/sdlc/zap-rules.conf`.
- **`Findings`** lists every alert (all severities) with sample affected URLs and
  ZAP's remediation hint — actionable without opening the HTML report.
- **`Report:`** is the full HTML/JSON/MD report on disk. `make stacktest-zap`
  writes it to **`./scratch/zap-reports/`** by default (scratch is gitignored, so
  a run never litters the tree); override with `REPORT_DIR=./somewhere`. Open
  `scratch/zap-reports/zap-report.html` in a browser for the full findings view.
  (The RBAC test similarly defaults to `./scratch/api-test-results/`.) In CI
  (SOURCE_BUCKET set) the report is also uploaded to S3 and shown as `Report (S3):`.
- The probe is **WARN-only**: it PASSES even with findings (0 High/FAIL is the
  expected clean state). To gate on findings, promote a rule WARN→FAIL in
  `zap-rules.conf` and flip the `# TODO promote` gate in `validate_zap_dast`.
- To tune what's reported: edit `scripts/sdlc/zap-rules.conf` (IGNORE known-noise
  plugin ids, or WARN/FAIL to change severity).

## VPC tests (`hosting-private`, `jobsapi`) — auto-discover a VPC, then CONFIRM

These need VPC wiring. Prefer discovering a suitable existing VPC in the account
over creating one. Procedure:

1. Look for a usable VPC/subnets/SG/endpoint in the target account:
   ```bash
   AWS_PROFILE=default aws ec2 describe-vpcs \
     --query 'Vpcs[].{Id:VpcId,Cidr:CidrBlock,Default:IsDefault,Tags:Tags}' --output json
   AWS_PROFILE=default aws ec2 describe-subnets --filters Name=vpc-id,Values=<vpc> \
     --query 'Subnets[].{Id:SubnetId,AZ:AvailabilityZone,Public:MapPublicIpOnLaunch}' --output json
   AWS_PROFILE=default aws ec2 describe-security-groups --filters Name=vpc-id,Values=<vpc> \
     --query 'SecurityGroups[].{Id:GroupId,Name:GroupName}' --output json
   AWS_PROFILE=default aws ec2 describe-vpc-endpoints --filters Name=vpc-id,Values=<vpc> \
     --query 'VpcEndpoints[?ServiceName==`com.amazonaws.<region>.execute-api`].VpcEndpointId' --output json
   ```
   Prefer a VPC that already has private subnets and an `execute-api` interface
   endpoint (the private API hosting test needs one). Check the pipeline's
   persistent test VPC first — it is purpose-built for this.
2. **Show the user what you found and CONFIRM before running** — never auto-pick
   and deploy into a VPC silently. Present the VPC id, subnets, SG, and endpoint
   you intend to use and ask them to approve.
3. On approval, run with make params (NOT env vars):
   ```bash
   AWS_PROFILE=default make stacktest-jobsapi \
     STACK_NAME=<stack> \
     VPC_ID=<vpc> SUBNET_IDS=<subnet-a,subnet-b> \
     LAMBDA_SG_ID=<sg> APIGW_VPCE_ID=<vpce>
   ```
4. If no suitable VPC exists, tell the user — offer to self-create one only if
   they ask (it is slow and adds VPC-quota pressure); do not create one by
   default.

## Notes

- These reuse the SAME deploy/validate/cleanup code as CI: the runner
  (`scripts/sdlc/run_stacktest.py`) imports `codebuild_deployment.py` and drives
  its internal deploy-variant table, so results match what CI would have produced
  — no drift.
- To temporarily re-enable the deploy-variant tests inside a pipeline run (rarely
  needed), set `IDP_RUN_PROBES=true`. Default is off.
- Permissions boundary is now verified only by the primary suite (Step 13,
  `test_step13_permission_boundaries`); the deploy-variant tests deploy without
  one.
