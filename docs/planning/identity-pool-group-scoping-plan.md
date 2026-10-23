# Plan: put the document bytes behind a group check

**Author:** design, 2026-09-21
**Status:** DECIDED and built. The shape chosen is option 2 below — the Identity
Pool's `authenticated` role grants no S3 at all, and every document read goes
through a resolver — together with two things the options as drafted did not
include: a **batch** presign field, so a document view costs one call rather than
one per page, and a **per-document scope check** on the configuration-profile
axis, so the resolver in the path is used for more than a bucket allow-list. The
API-side group floor that precedes it shipped first (issues #979 and
[#1033](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1033)).
§5 records what the decision leaves open, which is a question about this product's
data model rather than about this read path.
**Scope:** the Cognito Identity Pool's `authenticated` role, and the browser read
path it exists to serve. Not the REST API's per-operation authorization, which is
settled in `scripts/api_rbac_expectations.yaml` and documented in
[RBAC](../rbac.md).

---

## 1. What was measured

On a throwaway 0.6.10.dev1 stack, a self-signed-up Cognito account **in no group
at all** reached extracted personal data in five calls. Each was a separate
request by the same caller, and nothing was withheld or set up in advance.

| # | Call | Result |
|---|---|---|
| 1 | `listDocumentsDateShard` | **200** — `{"ObjectKey":"regression/lending_package.pdf","QueuedTime":"…"}` |
| 2 | `listDocumentVersions {objectKey}` | **200, 17,527 bytes** — `Sections[].OutputJSONUri`, `Pages[].ImageUri/TextUri/OcrPageDataUri`, the full `ConfidenceThresholdAlerts` list naming every extracted attribute, and `Pages[].ClassReason` |
| 3 | `getStepFunctionExecution` with the ARN from step 2 | **200, 317,614 bytes** — the execution input and step history |
| 4 | `getFileContents` / `getFilePresignedUrl` on the URI from step 2 | **403**, group-floor message |
| 5 | `cognito-identity get-id` + `get-credentials-for-identity`, then `s3api get-object` on the `OutputJSONUri` from step 2 | **rc=0, 15,953 bytes** of extracted output |

Two details of step 2 matter for what follows. The caller did **not** have to know
an object key — step 1 handed one over. And `Pages[].ClassReason` is model-written
prose about what the document contains; in this run it named *"employee personal
information (name, address, Social Security Number), pay period dates"*, so step 2
alone disclosed the sensitive categories present before any byte was fetched.

Step 5 is the subject of this document. The credentials it used came from
`get-credentials-for-identity` with the same groupless ID token, and resolved to
`assumed-role/…-CognitoAuthorizedRole-…/CognitoIdentityCredentials`. The object
body began `{"inference_result":{"CurrentNetPay":291.9,"EmployeeName":{"LastName":"…"},
"EmployeeAddress":{"Line1":"…"},…}}`.

All five steps are closed. Steps 1, 2 and 3 by the group floor: every operation in
the "Documents — read" section of `scripts/api_rbac_expectations.yaml` requires an
assigned group, and so do `getChatMessages` and `getCircuitBreakerStatus`. Step 4
was already closed, by #1023. **Step 5 is closed by the decision recorded here:**
the credentials `get-credentials-for-identity` vends resolve to a role that can
read no bucket, so `s3api get-object` on an `OutputJSONUri` is refused for every
signed-in caller, in a group or not.

---

## 2. Why the bucket grant, and not the API floor, was the boundary

`CognitoIdentityPoolSetRole` in `template.yaml` attaches exactly one role:

```yaml
CognitoIdentityPoolSetRole:
  Type: AWS::Cognito::IdentityPoolRoleAttachment
  Properties:
    IdentityPoolId: !Ref IdentityPool
    Roles:
      authenticated: !GetAtt CognitoAuthorizedRole.Arn
```

There is no `RoleMappings` key. Cognito therefore has nothing to map a
`cognito:groups` claim onto: every successfully authenticated identity assumes
`CognitoAuthorizedRole`, and an empty groups claim is as good as any other. So
anything that role can read is readable by every signed-in user irrespective of
group, with no resolver in the path — and therefore with no group check, no
per-document check and no per-user scope check available to apply to it. That is
what made the policy, and not the API surface, the boundary for everything it
named.

It named the Input and Output buckets, with `s3:GetObject` and
`s3:GetObjectVersion` on object ARNs plus five KMS actions on the customer-managed
key, because the UI signed those GETs in the browser. Four surfaces did so: the
document or section file in the file viewer, the full-size page image, every page
thumbnail, and the export bundle for those two buckets. Each now asks the resolver
for its URLs instead, and there is no longer a per-call-site choice to make: the
prop that selected between browser-side and resolver-side signing is gone from the
components, along with the browser presigner it dispatched to.

The role now grants **nothing** on the Input and Output buckets, and **no**
`s3:GetObject`, `s3:GetObjectVersion` or `s3:ListBucket` on any bucket at all.
Its inline policies are exactly two: `ChatStreamInvoke`, the identity-side grant
the browser's SigV4-signed call to the chat-streaming Function URL needs, and
`ParameterStore`, `ssm:GetParameter` on the one settings parameter the UI reads.
The KMS statement went with the S3 one: its only purpose was to let a
client-presigned GET decrypt an SSE-KMS object, and a URL the resolver mints is
decrypted under the resolver's role. `scripts/tests/test_browser_s3_grants.py`
pins the policy set in both directions, so this paragraph and the template cannot
drift apart silently.

Neither of the two buckets the per-user scope axes partition was ever a candidate
to stay: the Configuration bucket was removed so `allowedConfigVersions` could not
be read around, and the Test Set bucket was never on the role.

⚠️ **Do not read an `ANY_GROUP` declaration as making document content
unreachable.** It makes that *operation* unreachable. For the document read path
the two now coincide, because that path *is* three `ANY_GROUP` operations
(`getFileContents`, `getFilePresignedUrl`, `getFilePresignedUrls`) and there is no
second route to the bytes. For anything else the distinction still holds, and it
is the one most often got wrong when reading the expectations file.

---

## 3. The two candidate shapes

### Option 1 — group-scoped `RoleMappings` on the Identity Pool — not taken

Add `RoleMappings` to `CognitoIdentityPoolSetRole`, keyed on the user pool's
`cognito:groups` claim, and one IAM role per group. Browser-side signing survives
unchanged; the grant follows the group.

**What it would buy.** A caller in no group assumes a role with no S3 grant on the
document buckets, and step 5 of §1 fails. That is the whole of the measured gap.

**What it would not buy.** Nothing per-document. A Viewer's role would still carry
`s3:GetObject` across the whole Output bucket — object ARNs, but every object — so a
role mapping does not address UI.T06 (bucket-scoped, not key-scoped) or item 3 of
#979.

**The obstacle that decided it: the role set has to be designed against five
groups whose access is not uniform in the axis IAM can express.** Four of the five
(Admin, Author, Reviewer, Viewer) differ from one another by *which operations*
they may call, not by which objects they may read — so in bucket terms they
collapse to one role, and the mapping buys only the groupless case. The fifth does
not collapse:

> `Annotator` exists so ground-truth labeling can be delegated to people who should
> not see the rest of the deployment. It is the only role whose access is granted
> per-object rather than per-feature: an Annotator with no `allowedTestSets` can do
> nothing at all. — [RBAC](../rbac.md)

`allowedTestSets` is a per-user list of test set ids in the UsersTable, resolved at
request time by `idp_common/testset_scope.py::assert_can_access_test_set`. An IAM
role cannot resolve it: the value is not in the token, it varies per user rather
than per group, and it names test sets rather than prefixes. So an "Annotator role"
would have to grant the test-set bucket wholesale — broader than the Annotator's
actual entitlement, and a regression, because the Annotator path has never signed
in the browser. `AnnotationWorkspace.tsx` and `TestSetDocumentDetail.tsx` have
always gone to the resolver for their URLs, and the reason is structural rather
than a setting: the test-set bucket has never been in `CognitoAuthorizedRole`'s
resource list, so there is nothing for the browser to sign with. Any revisit of
this option has to leave that path alone rather than "unifying" it.

⚠️ If this option is ever revisited — as a defence-in-depth layer rather than as
the boundary — three things have to be decided before any of it is written:
whether an unauthenticated role is attached at all (there is none), what an
external-IdP user whose group claim is mapped by the pre-token trigger assumes,
and what a user holds between sign-up and group assignment. They currently hold a
working session with no entitlement; under role mappings they would hold one with
no credentials, which the UI has to distinguish from a broken deployment.

### Option 2 — narrow `CognitoAuthorizedRole` and route every read through a resolver — **taken**

Remove `s3:GetObject`/`GetObjectVersion` and the KMS grant from the browser's role
and make `getFileContents` / `getFilePresignedUrl` / `getFilePresignedUrls` the
only read path. The group floor then genuinely bounds the asset, and a per-key and
per-document check becomes possible because there is a resolver in the path to
make it.

**What it buys.** Everything option 1 buys, plus the place to put a per-document
check, plus as much of UI.T06 as this product's data model can express. This is the
shape that makes the API declarations load-bearing rather than advisory.

**What it cost, obstacle by obstacle.**

*It changes the document-viewing data path.* All four surfaces in §2 stopped
signing in the browser, and the choice was removed rather than re-defaulted — the
transport prop and the browser presigner behind it are both gone, so a call site
cannot opt back into client-side signing against a role that would refuse it. That
is a behaviour change on the hottest path in the UI, which is why §4 is a cost
measurement rather than a correctness note.

*Client-side signing existed partly because of the Lambda response size limit.*
The operations are not equivalent, and the difference is load-bearing:
`_handle_file_contents` returns the bytes in the Lambda response and so is capped
at the dispatcher's 6 MiB synchronous limit, while the presign handlers return a
URL and have no size limit. So this option does **not** move document bytes
through Lambda: every document read is routed through a presign field. What it
must not do is route them through `getFileContents`, which would break every file
over 6 MiB.

*A presigned URL is still a capability,* so moving the decision into a resolver is
not by itself a scope on the object. Three controls now run in
`_parse_and_validate_one`, the one function every field and every batch entry
passes through before S3 is touched: the dispatcher's field-level group floor, the
bucket allow-list, and `document_scope` — which resolves the document owning the
key from the tracking table and matches its `ConfigVersion` against the caller's
`allowedConfigVersions`. Without that third control this option would have closed
step 5 of §1 and left UI.T06 where it was.

*The resolver's own role becomes the broad one.* The grant did not disappear; it
moved to a principal the caller cannot assume. That is the point, and it means
`get_file_contents_resolver` is now the single component whose compromise yields
every document — which is why it is stated in the threat model rather than
discovered later.

---

## 4. Why the read path is batched

⚠️ **This is a cost argument, not a latency one, and the direction of travel is
easy to state backwards.** The page-image path previously made **zero** API round
trips: client presigning is local SigV4 in the browser, with no network call at
all. So routing it through a resolver goes 0→N, and the batch field makes it 0→1
or 2 rather than N→1. N was **twice** the page count, because two panels mounted
eagerly and each signed the whole page array; the 1 or 2 is one call per panel.
Nothing here is a round-trip reduction and none of it should be described as a
user-visible speedup — the N calls would have been issued in parallel.

What the batch field actually bounds:

* **Invocation count and cost.** One resolver invocation per document view instead
  of one per page, twice over.
* **Tail latency across N cold-startable invocations.** N parallel calls are as
  slow as the slowest of them, and any one of them may be a cold start.
* **Concurrency headroom.** A per-page call multiplies every concurrent document
  view by the page count against the account's Lambda concurrency.
* **Re-sign cadence.** A document being processed is polled every 4 seconds, and
  each poll re-signs; the multiplier applies on every tick, not once per view.

The sizing was measured offline against botocore in this worktree, with no AWS
calls:

| Measure | Value |
|---|---|
| Presigned URL length, 400-character session token | 891 bytes |
| …800-character token | 1,291 bytes |
| …1,200-character token | 1,691 bytes |
| 1,000 local signings | 0.357 s total |
| JSON response body, 6 pages | 8.8 KB |
| …27 pages (`samples/lending_package-long.pdf`, the largest sample) | 39.4 KB |
| …100 pages | 145.9 KB |
| …269 pages | 392.8 KB |
| Dispatcher's synchronous-invoke response cap | 6 MiB |

**No latency figure is available** — none of the above involves a deployed stack —
so the operation's wall-clock cost per call is unmeasured and should not be quoted.

Two caps bound a caller-supplied list, and they bound different resources.
`MAX_BATCH_OBJECTS = 1000` is the number of objects one call may name; a longer
list is **refused with a 400, not truncated**, because a truncated list returns
fewer URLs than were asked for and the caller renders blank pages with no error.
The number of **tracking-table round trips** one call may make is capped separately,
at `document_scope.MAX_DOCUMENT_LOOKUP_BATCHES = 64`, because those are DynamoDB
reads rather than local signings. A round trip resolves up to 100 candidate document
keys, and a batch over one document shares all but the two deepest candidates of each
of its page keys, so a 269-page view is six round trips however many pages it has — a
request needing more than 64 is not a document view. The UI chunks its own requests
well below the object cap, so both caps bound a hand-built request rather than a
document.

⚠️ **Past the round-trip cap the caller gets the ordinary refusal, not a "too broad"
error, and that is deliberate.** How many round trips a request costs depends on which
candidate documents exist, because the walk stops at the first one it finds — so a
distinguishable "too broad" status would hand a caller one bit per request about
whether an arbitrary object key is a tracked document, for documents entirely outside
their scope. That is the distinction every refusal message on this path is worded to
avoid, and it would need no timing analysis to exploit, because the two answers would
be different status classes.

One property of the batch is a security property rather than a cost one: an
authorization refusal fails the **whole** request instead of being reported per
entry. A per-entry refusal would turn one call into an N-key authorization oracle,
telling the caller key by key which documents are in their scope, and every entry
is authorized before the first URL is minted so that no partial result exists.

---

## 5. What this leaves open

The per-document axis enforced here is the configuration profile a document was
processed under, matched against the caller's `allowedConfigVersions`. That is the
only per-document axis this product records: there is **no document owner and no
tenant attribute** anywhere in the deployment, so "is this document yours" cannot
be asked, and the same pairing is what already bounds `listDocuments`,
`getDocumentCount`, `reprocessDocument` and document chat.

⚠️ **Read the consequence per caller shape, not per group.** "What a Viewer can
read" has no single answer here, because the axis is the caller's own scope row and
not their group:

* A **scoped** caller — a UsersTable row that sets `allowedConfigVersions` — is
  refused a document outside their profiles, and refused a key no tracked document
  owns, and refused a document carrying no `ConfigVersion`. All three fail closed.
* An **unscoped** caller — a row that sets no scope, or no row at all — is refused
  nothing by this check, because `resolve_allowed_config_versions` answers
  "unrestricted" for them and scoping is opt-in per user by design. Such a caller
  holding any one of the five groups can still read any document's bytes **through
  the resolver**, which is the same reach `listDocuments` already gives them.

So the net position is: the groupless hole is closed outright; the cross-document
read is closed for scoped callers and bounded by the group floor — not by document
ownership — for everyone else. Closing the rest is a **data-model** change, not a
read-path one: documents would need an ownership or tenancy attribute, and the
permitted key would be derived from it. UI.T06 in the threat model carries that
residual, and anyone proposing to close it should start there rather than here.
