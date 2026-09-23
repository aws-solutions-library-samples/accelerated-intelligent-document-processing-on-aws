# Plan: put the document bytes behind a group check

**Author:** design, 2026-09-21
**Status:** PROPOSAL. Nothing here is implemented. The API-side group floor that
precedes it is shipped (issues #979 and #1033); this document is about the part
that floor does not reach.
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

Steps 1, 2 and 3 are closed: every operation in the "Documents — read" section of
`scripts/api_rbac_expectations.yaml` now requires an assigned group, and so do
`getChatMessages` and `getCircuitBreakerStatus`. Step 4 was already closed, by #1023.
**Step 5 is open and is not affected by any of that.**

---

## 2. Why the API floor is not a control on the document bytes

`CognitoIdentityPoolSetRole` in `template.yaml` (line ~11427) attaches exactly one
role:

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
`CognitoAuthorizedRole`, and an empty groups claim is as good as any other. That
role's `S3` policy grants `s3:GetObject`, `s3:GetObjectVersion` and `s3:ListBucket`
on the Input and Output buckets and their contents, plus five KMS actions
(`Encrypt`, `Decrypt`, `ReEncrypt*`, `GenerateDataKey*`, `DescribeKey`) on the
customer-managed key. Neither of the two buckets the per-user scope axes partition
is on it: the Configuration bucket was removed so `allowedConfigVersions` could not
be read around, and the Test Set bucket was never there.
`scripts/tests/test_browser_s3_grants.py` pins that set, so this paragraph and the
policy cannot drift apart silently.

So the API floor and the bucket grant are two independent paths to the same bytes,
and the floor is on the one the UI does **not** use by default. This is not a
bypass or a misconfiguration: browser-side signing is the shipped read path.
`FileViewer` defaults to `presignVia = 'client'`
(`src/ui/src/components/document-viewer/FileViewer.tsx:79`), and four call sites
sign S3 GETs in the browser with those credentials:

| Call site | What it fetches |
|---|---|
| `src/ui/src/components/document-viewer/FileViewer.tsx:194` | the document or section file being viewed |
| `src/ui/src/components/common/PageImageViewer.tsx:283` | the full-size page image |
| `src/ui/src/hooks/use-page-thumbnails.ts:50` | every page thumbnail |
| `src/ui/src/components/document-panel/document-export.ts:234` | the export bundle, when the bucket is the Input or Output bucket |

⚠️ **Do not read an `ANY_GROUP` declaration as making document content
unreachable.** It makes that *operation* unreachable. The correct statement of
what #979 and #1033 bought is: a caller in no group can no longer discover keys,
enumerate runs, read a run's URIs and confidence alerts, read the workflow
execution, read a chat transcript, or ask the API for file bytes. A caller in no
group who obtains a key by any other means — being told one, guessing a
predictable upload name, or `s3:ListBucket` on their own role — can still read the
object. `s3:ListBucket` is the important one: it makes the key enumeration
self-contained on the S3 side, so closing the API-side enumeration narrowed the
chain without severing it.

---

## 3. The two candidate shapes

### Option 1 — group-scoped `RoleMappings` on the Identity Pool

Add `RoleMappings` to `CognitoIdentityPoolSetRole`, keyed on the user pool's
`cognito:groups` claim, and one IAM role per group. Browser-side signing survives
unchanged; the grant follows the group.

**What it buys.** A caller in no group assumes a role with no S3 grant on the
document buckets, and step 5 of §1 fails. That is the whole of the measured gap.

**What it does not buy.** Nothing per-document. Every Viewer still holds
`s3:GetObject` on the entire Output bucket, so a role mapping does not address
UI.T06 (bucket-scoped, not key-scoped) or item 3 of #979.

**The real obstacle: the role set has to be designed against five groups whose
access is not uniform in the axis IAM can express.** Four of the five (Admin,
Author, Reviewer, Viewer) differ from one another by *which operations* they may
call, not by which objects they may read — so in bucket terms they collapse to one
role, and the mapping buys only the groupless case. The fifth does not collapse:

> `Annotator` exists so ground-truth labeling can be delegated to people who should
> not see the rest of the deployment. It is the only role whose access is granted
> per-object rather than per-feature: an Annotator with no `allowedTestSets` can do
> nothing at all. — [RBAC](../rbac.md)

`allowedTestSets` is a per-user list of test set ids in the UsersTable, resolved at
request time by `idp_common/testset_scope.py::assert_can_access_test_set`. An IAM
role cannot resolve it: the value is not in the token, it varies per user rather
than per group, and it names test sets rather than prefixes. So an "Annotator role"
would have to grant the test-set bucket wholesale — which is broader than the
Annotator's actual entitlement and would be a regression, because the Annotator
path deliberately does **not** sign in the browser today. `AnnotationWorkspace.tsx`
and `TestSetDocumentDetail.tsx` both pass `presignVia="server"`, and the reason is
structural: the test-set bucket is not in `CognitoAuthorizedRole`'s resource list at
all, so there is nothing for the browser to sign with. Option 1 must leave that
path alone rather than "unifying" it.

Also to decide before writing any of it: whether an unauthenticated role is
attached at all (there is none today), what an external-IdP user whose group claim
is mapped by the pre-token trigger assumes, and what happens to a user between
sign-up and group assignment — they currently hold a working session with no
entitlement, and under this option they would hold one with no credentials, which
the UI has to distinguish from a broken deployment.

### Option 2 — narrow `CognitoAuthorizedRole` and route every read through a resolver

Remove `s3:GetObject`/`GetObjectVersion`/`ListBucket` (and the KMS grant) from the
browser's role, and make `getFilePresignedUrl` / `getFileContents` the only read
path. The group floor then genuinely bounds the asset, and a per-key check becomes
possible because there is a resolver in the path to make it.

**What it buys.** Everything option 1 buys, plus the place to put a per-document
check, plus closure of UI.T06 once that check exists. This is the shape that makes
the API declarations load-bearing rather than advisory.

**The real obstacles.**

*It changes the document-viewing data path at the four call sites in §2.* Each has
to stop calling `generateS3PresignedUrl` and start calling the resolver; `FileViewer`'s
default flips from `'client'` to `'server'`. That is a behaviour change on the
hottest path in the UI — a document with 40 pages requests 40 thumbnails — so it
needs its own performance measurement, not just a correctness test.

*Client-side signing exists partly because of the Lambda response size limit.* The
two operations are not equivalent, and the difference is load-bearing here:
`_handle_file_contents` returns the bytes in the Lambda response and is therefore
capped at Lambda's 6 MB synchronous limit, while `_handle_presigned_url` returns a
URL and has no size limit — it is documented in
`nested/api-resolvers/src/lambda/get_file_contents_resolver/index.py:149-160` as
existing for exactly the files `getFileContents` cannot carry. So option 2 does
**not** have to move document bytes through Lambda: it can route every read through
`getFilePresignedUrl`, paying one `HeadObject` plus a signing round trip per object
instead. What it must not do is route them through `getFileContents`, which would
break every file over 6 MB.

*A presigned URL is still a capability.* Server-side signing moves the decision
into a resolver but does not by itself scope the URL to one key — that is a second
change, and `_validate_bucket` (an allow-list of the stack's own buckets) is what
stands in for it today. Without the key-scoping step, option 2 closes step 5 of §1
and leaves UI.T06 open.

*The resolver's own role becomes the broad one.* The grant does not disappear; it
moves to a principal the caller cannot assume. That is the point, but it means the
resolver is now the single component whose compromise yields every document, which
is worth stating in the threat model rather than discovering later.

### Recommendation

Option 2 is the one that ends the class of problem, and option 1 is the one that
closes the measured gap without touching the data path. They are not mutually
exclusive: option 1 could ship first as a narrowing of blast radius, provided it is
described honestly — as removing the groupless caller from the bucket grant, not as
scoping documents to their owner. Whichever goes first needs a deploy-tested
migration story for existing stacks, because both change an IAM role that live
browser sessions are already using.

---

## 4. What the API change does and does not buy

This is the part most likely to be misread, so it is stated flatly.

**Does:** eighteen API operations refuse a caller in no group — the eleven from
#1023 plus `getDocumentCount`, `listDocumentsDateHour`, `listDocumentsDateShard`,
`listDocumentVersions`, `getStepFunctionExecution`, `getChatMessages` and
`getCircuitBreakerStatus`. A self-registered account can no longer use the API to
find out that a document exists, what it is called, how many there are, which
processing runs it has, where its section output and page images live, what
attributes were extracted from it, what the model said its pages contain, how its
workflow ran, or what a chat session said about it — nor read an administrator's
email address out of the processing breaker's last-error field.

**Does not:** stop that account reading the document bytes out of S3. Steps 1–4 of
§1 are closed and step 5 is unchanged. The bytes are behind an S3 bucket policy and
an IAM role, and neither consults a Cognito group.

A reader who takes "the document-content operations require a group" to mean "the
documents require a group" will size their risk wrongly. The floor is worth having
— it removes the self-contained, no-prior-knowledge chain that #1033 measured, and
it is the layer a future per-document check will hang off — but the asset is not
behind it yet.
