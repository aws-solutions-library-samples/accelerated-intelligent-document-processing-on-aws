# Seller Entitlement Service

Deploy this in **your AWS Marketplace seller account** to gate a paid Feature
Platform extension. It issues short-lived, account-bound activation tokens to
extension deployments running in buyers' accounts.

It is generic — any seller of any Feature Platform extension can deploy it as-is
and register their own product ids. Nothing here is specific to a particular
extension.

> **Deploy target:** the seller account, once. **Not** a customer account, and
> **not** part of the IDP Accelerator main stack.

## Why this exists

A Feature Platform extension deploys into the **buyer's** AWS account. The buyer
owns the Lambda, its environment variables, its IAM role, and its code.

> Software executing in the customer's own AWS account cannot enforce its own
> licence.

So the host's entitlement check is advisory by design, and `uiAccessAllowed` /
`entitlementVerified` in `FeatureContext` are signals to *warn* on, not to gate
on. See
[the developer guide](../../docs/feature-platform-developer-guide.md#entitlement-enforcement-is-the-extensions-job).

Enforcement requires **the seller to hold something the buyer needs at runtime**.
This service is that thing: it runs where the seller controls both the code and
the answer, and it is also the only place the relevant Marketplace APIs work.
`SearchAgreements` with `PartyType=Proposer`, `GetEntitlements`, and
`ResolveCustomer` are all seller-side. Called from a buyer account they return an
*empty result rather than an error*, which is exactly how a buyer-side gate ends
up silently denying every real customer.

## Deploy

```bash
idp-feature-cli seller-service deploy \
  --product-registry '{"prod-a5ee62vs2xa72":{"productCode":"q0k0s3zuuga46hle6fecx547","allowFreeTier":true}}'
```

Or, from a repo checkout, the equivalent `make` wrapper:

```bash
make seller-entitlement-service \
  PRODUCT_REGISTRY='{"prod-a5ee62vs2xa72":{"productCode":"q0k0s3zuuga46hle6fecx547","allowFreeTier":true}}'
```

**Deploys into whichever account your credentials resolve to**, so it runs a
preflight first and refuses if that account does not own the products you are
registering. Check it any time, read-only:

```bash
idp-feature-cli seller-service preflight --product-registry '{...}'
```

The preflight verifies **ownership**, not merely "is this a seller account" — an
account-id comparison would pass for any seller, including one that doesn't sell
this product. It exists because deploying into the wrong account fails *silently*:
`SearchAgreements(PartyType=Proposer)` answers only for the product's owner and
returns an empty list rather than an error, so every activation would be refused
and every customer locked out with nothing in the logs explaining why.

Useful options: `--seller-account-id` to assert the expected account,
`--stack-name`, `--region` (default `us-east-1`), `--allowed-accounts`,
`--token-ttl-seconds`, `--yes` to skip the confirmation, and
`--skip-ownership-check` for the rare case where the deploying role lacks
`aws-marketplace:ListEntities`.

Requires the AWS SAM CLI and a checkout of this repository (the template and
Lambda source live here) — the same prerequisites as `idp-feature-cli publish`
and `init`.

`productId` is the SaaS product **entity id** (`prod-…`) — that is what
`SearchAgreements` matches on, not the product code. Find it with:

```bash
aws marketplace-discovery get-listing --listing-id prodview-XXXX --region us-east-1 \
  --query 'associatedEntities[0].product.productId' --output text
```

The deploy ends by reading the product registry back off the deployed Lambda and
failing if it did not arrive intact. That check is not paranoia: a mangled registry
produces an endpoint that deploys clean, answers every request, and refuses every
activation as an unknown product — which this service deliberately makes
byte-identical to "not subscribed", so neither a smoke test nor
`tests/dynamic_activation_test.py` can tell the two apart. Registry problems have
to be caught at deploy time or not at all.

> **Pass the registry through the CLI, not `sam deploy` by hand.** SAM re-tokenizes
> `--parameter-overrides` with its own quote-aware parser and silently truncates an
> unquoted value at the first `"`, so a hand-rolled
> `sam deploy --parameter-overrides ProductRegistryJson={"prod-…":…}` delivers a
> registry of exactly `{`. The CLI compacts the JSON and single-quotes it.

### Two account-level prerequisites the template handles for you

Worth knowing about, because both are account-global rather than stack-local:

- **API Gateway CloudWatch role.** A REST stage with access logging is rejected
  outright unless the account-level role is set, so the stack creates
  `AWS::ApiGateway::Account` — a per-account, per-region singleton. It is
  `DeletionPolicy: Retain`, so deleting this stack will *not* clear the setting out
  from under other APIs in the account; the corollary is that a teardown leaves the
  role behind by design.
- **`InvokeRole: NONE`.** SAM would otherwise default the `AWS_IAM` authorizer to
  invoking the Lambda with *caller* credentials, which both conflicts with the
  resource policy (the deployment 400s) and could never work cross-account, since
  buyers do not hold `lambda:InvokeFunction` on your function.

Then note the stack outputs:

```bash
aws cloudformation describe-stacks --stack-name idp-seller-entitlement \
  --query 'Stacks[0].Outputs' --output table
```

- `ActivationEndpoint` — bake into your published extension template.
- `TokenPublicKeyCommand` — run it to get the **public** verification key. Safe to
  embed in your published (public-read) artifacts: it verifies tokens, it cannot
  mint them.
- `RequiredBuyerPermission` — the `execute-api:Invoke` grant your extension's
  Lambda role needs in the buyer's account.

## How a request is authenticated

This is the part that has to be right, so it is worth being explicit.

The API method uses **`AWS_IAM` authorization**, with a resource policy that
admits any AWS principal. API Gateway verifies the caller's SigV4 signature
*before* invoking the Lambda and reports the verified account in
`requestContext.identity.accountId`.

- `Principal: '*'` means **any authenticated AWS caller may attempt activation** —
  not anonymous access. An unsigned request is rejected with 403 before it reaches
  the function.
- The Lambda reads the buyer account **only** from `requestContext.identity`,
  never from the request body. A body field would be trivially spoofable, which
  would let anyone claim to be a subscribed account and defeat the whole service.
- The seller therefore does not need to know buyer account ids in advance, and
  buyers need no credentials from the seller. This is what makes it work for an
  arbitrary, unknown set of customers.

## The endpoint URL is indirected — do not hard-code it alone

`ActivationEndpoint` contains the API-Gateway-assigned REST API id
(`https://<apiId>.execute-api.<region>.amazonaws.com/prod/activate`). If that API
is ever replaced — a stack rebuild, a region move, an account migration — the id
changes. Installed extensions are running in **customer** accounts you cannot
reach to update, so a baked-in URL would be permanent in the worst way: the day
you need to move, every paying customer breaks at once.

So the endpoint is published as a small pointer object beside `latest.json`, in
every region the extension is offered in (an extension reads from its own regional
bucket):

```bash
idp-feature-cli seller-service publish-endpoint \
  --feature-id idp-auto-optimizer \
  --bucket-basename aws-ml-blog \
  --artifact-regions us-east-1,us-west-2,eu-central-1 \
  --prefix artifacts/genai-idp-mp
```

Writes `…/extensions/<featureId>/activation.json`:

```json
{
  "schemaVersion": "1.0",
  "activationEndpoint": "https://xxxx.execute-api.us-east-1.amazonaws.com/prod/activate",
  "signingKeyId": "arn:aws:kms:us-east-1:…:key/…",
  "serviceVersion": "0.6.5.dev1",
  "publishedAt": "2026-08-20T…Z"
}
```

Re-run it after any change that replaces the API. Run it **before first publish**
— indirection added later cannot help bundles already shipped.

### Getting the values to bake in

Extension authors should not copy the endpoint or the key out of a console. One
command emits everything a release needs to embed:

```bash
idp-feature-cli seller-service export-trust-bundle --output-dir ./trust
# -> trust/activation-trust.json, trust/activation-public-key.pem
```

```json
{
  "schemaVersion": "1.0",
  "activationEndpoint": "https://xxxx.execute-api.us-east-1.amazonaws.com/prod/activate",
  "kid": "arn:aws:kms:us-east-1:…:key/…",
  "signingAlgorithm": "RSASSA_PSS_SHA_256",
  "publicKeyPem": "-----BEGIN PUBLIC KEY-----\n…",
  "serviceVersion": "0.6.5.dev1",
  "exportedAt": "…"
}
```

`kid` is the signing key **ARN** — an identifier, not the key itself. It is
byte-identical to the token's `kid` claim, which is what lets a verifier pick the
right embedded key when two are trusted during a rotation. Omit `--output-dir` to
print the JSON to stdout for a release script to consume. It reads the endpoint and
key ARN from the stack and the public key from KMS, so a stale value cannot be
baked in by accident; it needs `kms:GetPublicKey` under the seller's own
credentials, which the activation Lambda deliberately does not have.

So the per-release order is:

1. `seller-service export-trust-bundle` → embed the endpoint, `kid`, and public key.
2. `idp-feature-cli publish` → upload the extension artifacts and `latest.json`.
3. `seller-service publish-endpoint` → publish/refresh `activation.json`.

### It carries no key material, on purpose

The public verification key stays **embedded in the extension**. It is deliberately
not in the pointer, and `publish_activation_pointer` refuses to write a document
containing key material.

The reason is the trust model. If the pointer carried the key, then whoever can
write to the artifact bucket could substitute a hostile endpoint *and* the key that
validates its tokens — the bucket would become a forgery trust root and the whole
gate would be worthless. With key material excluded, a tampered pointer can only
redirect the request somewhere that cannot produce a signature verifying against
the embedded key, so the activation **fails closed** and the buyer-side grace
period absorbs it. Worst case is denial of service, not free access.

`signingKeyId` is a **hint only** — for choosing among keys the extension already
embeds during a rotation (the token's `kid` claim is the other half of that). Never
treat it as a key, and never fetch a key from it.

### Why not CloudFront or a custom domain?

A CloudFront distribution in front of this API does not work cleanly: the API uses
`AWS_IAM`, SigV4 signs the `Host` header, and a client signing for the CloudFront
domain is rejected by API Gateway because it does not own that hostname — while
signing for the `execute-api` host puts the API id straight back into the client.
The CloudFront domain is also itself an AWS-assigned identifier, so it relocates
the permanence problem rather than removing it.

A **custom domain** (`AWS::ApiGateway::DomainName` + `BasePathMapping`) *is* the
better end state — fully transparent to installed clients, no client-side logic —
but it needs a domain delegated to the seller account plus an ACM certificate. When
one is available, add it and simply publish the domain URL in the pointer; the
extension side never changes, which is exactly what makes the pointer the migration
lever for that cutover.

## Buyer-side integration contract

In your published extension template:

1. Grant the extension's Lambda role `execute-api:Invoke` on the activation
   endpoint (see the `RequiredBuyerPermission` output). Grant it on the **API id
   wildcard** rather than one literal id if you can, so a repointed endpoint does
   not also need an IAM change in the customer's account.
2. Resolve the endpoint from `activation.json` (above), cache it, and fall back to
   the URL compiled into the bundle if the pointer is unreachable. Then, on startup
   and on a schedule shorter than `TokenTtlSeconds`, POST `{"productId": "prod-…"}`
   to it with SigV4.
3. Verify the returned token with the embedded **public** key, and check
   `buyerAccountId` matches the account you are running in and `exp` is in the
   future.
4. **Cache the last-known-good token and apply a grace period longer than the
   TTL.** This is not optional. Without it, an outage in *your* service breaks
   *your paying customers*, which is a worse failure than briefly serving an
   unsubscribed one.
5. Gate on `freeTier` if your listing has a free dimension — an unsubscribed
   account gets a `freeTier: true` token when `allowFreeTier` is set, so it can
   run in reduced mode rather than being refused outright.

### Response

```json
{
  "token": "<base64 JSON claims>",
  "signature": "<base64 RSASSA_PSS_SHA_256 over the raw claims bytes>",
  "signingAlgorithm": "RSASSA_PSS_SHA_256",
  "expiresAt": "2026-08-19T15:00:00Z",
  "freeTier": false
}
```

Claims: `{productId, buyerAccountId, freeTier, iat, exp}`.

Failures: `403 not_entitled`, `400` (malformed body), `413` (body over 4 KB),
`401 unauthenticated` (API misconfigured — should be impossible with `AWS_IAM`).

Note there is **no distinct status for an unregistered product**: "unknown
product" returns the same `403` and the same body as "no active agreement", on
purpose, so the endpoint is not a product-existence oracle for anyone with an AWS
account. Do not branch on it — you will never see it. The corollary is that a
misconfigured registry is indistinguishable from an unsubscribed customer from
out here, which is why the registry is verified at deploy time instead.

## Who has activated? (visibility)

Every activation attempt — granted **and** refused — is recorded three ways.

**1. The activation roster (the durable record).** One DynamoDB item per (buyer
account, product): first/last seen, attempt and grant counts, last outcome and
reason, free-tier flag, and the service version that answered. Read it with your
seller credentials:

```bash
idp-feature-cli seller-service activations                     # everything, newest first
idp-feature-cli seller-service activations --product-id prod-a5ee62vs2xa72
idp-feature-cli seller-service activations --outcome refused   # unentitled attempts
idp-feature-cli seller-service activations --buyer-account-id 111122223333
idp-feature-cli seller-service activations --since 2026-08-01 --json
```

A `--product-id` read uses the `ProductIndex` GSI rather than scanning. The table
is `Retain`-on-delete and has point-in-time recovery: it is a record of who your
customers are.

Writes are **fail-open** — if the roster write fails the token is still issued.
Bookkeeping must never be the reason an entitled customer is refused.

**2. CloudWatch metrics.** `ActivationAttempt` in namespace
`IDPSellerEntitlement`, dimensioned by `ProductId` + `Outcome`
(`granted`/`refused`) — emitted via Embedded Metric Format, so no extra IAM and no
added latency on the activation path. Alarm on a rising `refused` count.
(`NotEntitledActivations`, from the API access log, additionally counts 403s that
never reached the Lambda.)

**3. Logs, for per-request forensics.** `/aws/lambda/<fn>` has the decision detail;
`/aws/apigateway/<stack>-activation` has one JSON line per request with the
verified caller account and status. Both age out with `LogRetentionInDays`
(default 90) — which is exactly why the roster exists.

> **No UI, deliberately.** This service runs in the *seller's* account; the IDP
> web UI belongs to the *buyer's* stack, so there is nowhere in the product to put
> a seller-facing console. The CLI is the surface; CloudWatch has the graphs.

## Fail-closed here, grace-period there

Note the deliberate asymmetry:

| Where | On error | Why |
|---|---|---|
| **Host** (`checkFeatureEntitlement`) | allow (advisory) | Runs in the customer's account; an error usually means a missing IAM grant. Denying would lock out a paying customer over *our* misconfiguration. |
| **This service** | deny | Runs in the seller's account; an error means the seller's own infrastructure is broken. Minting tokens on our own failure makes the gate meaningless. |
| **Extension** (token check) | grace period | Bridges a seller-side outage without permanently accepting an unverified state. |

## Threat model — read this before claiming it is enforced

**What it stops.** A customer flipping a CloudFormation parameter
(`FeaturePlatformSubscriptionMode=auto`), pointing the host at a marketplace
simulator, or deploying the extension straight from its public-read template to
skip the host UI. None of those produce a valid activation token.

**What it does not stop.** A customer who modifies the extension. They own the
Lambda; they can delete the token check. This design raises the effort from "one
parameter" to "reverse-engineer and patch the product", and produces a reliable
seller-side activation record for commercial follow-up. That is deterrence plus
evidence — not tamper-proofing, and it should not be described as such.

**What makes it actually bite.** The token must gate something the customer
genuinely needs *from the seller*, fetched at runtime. Candidates, strongest
first:

1. Seller-hosted execution of the valuable logic.
2. A seller-hosted planner/scoring service the extension calls per operation.
3. Prompt / strategy / model-routing configuration fetched from the seller.

If the token only unlocks a local boolean, a patch removes it and you are back to
nothing. **Choosing what the token gates is a product decision, not an
engineering one**, and it determines whether this service is enforcement or
theatre.

**Also worth knowing:**

- Activation logs (Lambda + API access log, retained per
  `LogRetentionInDays`) record which accounts activated which product, including
  refusals. That is the reconciliation trail.
- A `NotEntitledActivations` metric counts 403s.
- Tokens are bound to the buyer account, so they cannot be shared between
  customers. TTL bounds how long a cancelled subscription keeps working.
- The signing key is `Retain` on stack delete: destroying it would invalidate
  every issued token and make previously-issued ones unverifiable.
- `AllowedAccounts` bypasses the subscription check entirely. Every entry is an
  account getting your paid product for free — use it for your own test
  deployments and keep it empty in production.

## Metering (not included, but design it together)

A paid SaaS listing bills through seller-side `BatchMeterUsage`, which needs the
same buyer→seller channel this service establishes. Adding a `/meter` endpoint
here — same authentication, same verified caller account — is the natural next
step, and it means enforcement and billing share one piece of infrastructure
rather than two. `ResolveCustomer` is already in the Lambda's IAM policy for the
SaaS registration flow.

## Security testing and scanner triage

**Static (offline, runs with the unit tests).**
`tests/test_template_security.py` asserts 16 security invariants that live in the
CloudFormation template, where handler unit tests cannot see them and `cfn-lint`
only checks syntax: SigV4 required, no `Authorization: NONE`, resource policy
pinned to `POST /activate`, throttling and concurrency set, marketplace grants
read-only, `kms:Sign` only, `dynamodb:UpdateItem` only, asymmetric key, no
wildcard key-policy principal, retain policies, SSE + PITR, no hardcoded account
ids. Each was **mutation-tested** — flipping `AWS_IAM`→`NONE`,
`UpdateItem`→`dynamodb:*`, adding `kms:PutKeyPolicy` or `BatchMeterUsage`,
widening the resource policy, disabling SSE, or planting an account id each makes
a specific test fail.

**Dynamic (live, against a deployed stack).**
`tests/dynamic_activation_test.py` asserts what the deployed *stage* does, which
can differ from the template after a console edit or a partly-applied change set:
unsigned → 403, unentitled → 403 with no internal detail, unknown product →
byte-identical to unentitled, malformed body → 400 (not 502), oversized → 413,
and — with a subscribed account — a token that verifies against the published
public key, is bound to the calling account, and carries `kid`.

**Not applicable, deliberately.** The repository's ZAP DAST and RBAC harnesses do
not fit this service and should not be stretched to: ZAP cannot SigV4-sign, so it
could only ever confirm "everything is 403" (already covered by one line of the
dynamic test), and the RBAC harness is built around Cognito groups on the host's
`/op/<field>` API, which this service has none of.

### IaC scanner findings — accepted or rejected, with reasons

Generic IaC checks flag several things here. Recorded so they are triaged once:

| Check | Decision |
|---|---|
| Lambda concurrency limit (`CKV_AWS_115`) | **Fixed.** `ReservedConcurrency` added — a real control for SELL.T04, since the endpoint is open to any AWS account. |
| API Gateway caching (`CKV_AWS_120`) | **Rejected, and guarded by a test.** A cached activation response would keep answering "entitled" after a cancellation. Enabling it would turn a revenue control into a stale one. |
| Lambda in a VPC (`CKV_AWS_117`) | **Rejected.** The function must reach public AWS APIs (Marketplace, KMS, DynamoDB); a VPC adds NAT/endpoints for no security gain. |
| Lambda DLQ (`CKV_AWS_116`) | **N/A.** Invocation is synchronous via API Gateway; DLQs apply to async invocations. |
| Log group KMS CMK (`CKV_AWS_158`) | **Fixed.** Originally accepted on the grounds that AWS-managed encryption applies — but that contradicted this repository's own standard (115 of 135 log groups elsewhere use a CMK), and these logs carry buyer account ids and caller role ARNs. A dedicated `LogEncryptionKey` now encrypts both log groups, with the CloudWatch Logs grant scoped by encryption context. |
| Lambda env var KMS CMK (`CKV_AWS_173`) | **Accepted.** Env vars hold a product registry and an account allow-list, not secrets, and AWS already encrypts them at rest with an AWS-managed key. |
| DynamoDB CMK (`CKV_AWS_119`) | **Accepted.** `SSEEnabled: true` uses an AWS-managed key. The roster holds AWS account ids, not credentials or document content. Switch to a CMK if your data-classification policy requires customer-managed keys. |
| API Gateway X-Ray (`CKV_AWS_73`) | **Optional.** Observability rather than security; enable if you want traces. |

### CI coverage, and what cannot be automated

**In CI, per commit (offline).** `make test-packages-cicd` — which the
`developer-tests` workflow now calls — runs this service's full suite: handler
behaviour, the 16 template-security invariants, and the payload-robustness corpus.
No AWS, no credentials, no cost. That corpus is the highest-value part: it caught a
crash-on-hostile-input bug **in a security fix**, where a non-string `productId`
hit `.strip()` before the type check, making the type guard unreachable dead code.

**Manual, against a live stack.** `tests/dynamic_activation_test.py` (see above).
Deliberately not per-commit, for reasons worth stating rather than discovering:

- **The preflight refuses to deploy in a CI account** — correctly. A build account
  owns no Marketplace SaaS product, so `ListEntities` finds nothing and the
  ownership check fails. Deploying in CI means passing
  `--skip-ownership-check`, i.e. testing with the safety guard switched off.
- **The positive path cannot be automated in a build account.** Issuing a real
  token needs product ownership *and* a genuinely subscribed buyer account. CI can
  cover the negative and robustness assertions (unsigned → 403, hostile payload →
  4xx, oracle equality); "token issued and verifies" needs the seller account.
- **Teardown leaks retained resources.** `TokenSigningKey` and `ActivationsTable`
  are `DeletionPolicy: Retain` — right for production, since destroying the key
  invalidates every issued token — and `DeletionPolicy` takes a literal, so it
  cannot be relaxed per-environment with `Fn::If`. A plain `delete-stack` therefore
  orphans a KMS key and a table on **every** run, and KMS has no immediate delete
  (7–30 day pending window, billed throughout). Use
  `tests/teardown_test_stack.sh`, which captures the retained physical ids before
  deleting the stack and then cleans them up. It refuses any stack name that does
  not look like a test stack, because scheduling deletion of a production signing
  key is not something a cleanup script should be able to do by accident.

**Why not ZAP/DAST.** The goal — "hostile payloads must not elicit a response or
break the service" — is right, and was a real bug here. But ZAP cannot reach the
code: it does not SigV4-sign, so every request it sends is rejected by the
`AWS_IAM` authorizer with 403 *before the Lambda is invoked*. An active scan would
report a clean 403 wall and prove nothing about the parser. The same goal is met
directly by the payload corpus — offline against the handler in CI, and over signed
HTTP against a deployed stage in the dynamic test. If a scanner run is needed for
compliance evidence, the honest way is a SigV4-signing forward proxy in front of
ZAP; that is a bespoke harness, and it would be testing what the corpus already
covers.

### SAST results

`semgrep` (one of SRT's engines) reported **one** finding on this code and it was a
false positive: `logging.logger-credential-leak` fired because a log *message*
contained the word "token", while its arguments were only identifiers and
counters. Rather than suppress it, the message was reworded to say what happened
("Activation granted") and the invariant was locked in properly by
`test_no_logger_call_receives_token_material`, which inspects the **arguments** of
every `logger.*` call by AST. That is a stronger control than the scanner's
message-text heuristic — it catches the signed payload being added to an existing
log line whose wording looks innocuous, which the heuristic would miss. Semgrep is
now clean on both files.

`bandit` reports `B404`/`B603` on the `sam` subprocess call, annotated inline with
the repo's `# nosec` convention: fixed argv, no `shell=True`, and each
operator-supplied value is a single argv element that cannot split into extra
arguments.

The full SRT scan also runs automatically on merge requests and may surface
findings under its own rule names; triage them there, adding suppressions to
`scripts/srt/issues.json` with a rationale rather than silencing them locally.

## Verification status

The seller-side entitlement query is **verified against a live seller account**,
with both controls:

| Case | Result |
|---|---|
| Subscribed buyer account | returns its `ACTIVE` `PurchaseAgreement` (open-ended, `endTime: null`) |
| Unsubscribed buyer account | returns an empty list — **not** an error |

Two things that verification settled, and that you should not "fix" back:

- The filter name is **`ResourceIdentifier`**. The AWS docs' prose for the
  Proposer-side combination list says `ResourceId`, and the live service rejects
  that with `ValidationException: Provided filter name is invalid`. `FilterName`
  is a free-form string with no client-side validation, so only a real call
  reveals this.
- **`GetEntitlements` returns `{"Entitlements": []}` from every caller** — the
  seller account with the correct product code, an unsubscribed buyer, and a
  genuinely **subscribed** buyer. Confirmed, not inferred. A usage-based SaaS
  listing has no entitlement records at all, so that API can never answer "is this
  buyer subscribed?" from anywhere. It remains in the IAM policy only for SaaS
  *Contract* listings.

Verified against the live listing across **every** caller/subscription combination:

| Caller | Subscribed to this product? | `GetEntitlements` | `SearchAgreements` |
|---|---|---|---|
| Buyer account | no | `[]` | `[]` (correct negative) |
| Buyer account | **yes** | **`[]`** | **ACTIVE agreement** (correct positive) |
| **Seller** account | n/a (product owner) | **`[]`** | ACTIVE agreement via `PartyType=Proposer` |

`GetEntitlements` returns an empty list in **every** case — including from the
seller account, and including for a genuinely subscribed buyer. There is no
configuration in which it answers this question for a usage-based SaaS listing,
because such a listing has no entitlement records at all. `SearchAgreements`
answers correctly in every case.

