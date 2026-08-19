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

## Buyer-side integration contract

In your published extension template:

1. Grant the extension's Lambda role `execute-api:Invoke` on the activation
   endpoint (see the `RequiredBuyerPermission` output).
2. On startup, and on a schedule shorter than `TokenTtlSeconds`, POST
   `{"productId": "prod-…"}` to `ActivationEndpoint` with SigV4.
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

Failures: `403 not_entitled` (no active agreement), `404 unknown product`,
`401 unauthenticated` (API misconfigured — should be impossible with `AWS_IAM`).

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

