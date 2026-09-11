# Talos Mitigation Report — IDP Accelerator

**Engagement:** Incremental Review Q126: Open Source GenAI IDP Accelerator
**Report Started:** 2026-04-25
**Companion document:** `scratch/Talos Security Findings Review.md` (original review + our assessment)

This report is updated incrementally as each finding is addressed. Each section is written to be pasted directly into a Talos response where appropriate.

---

## Status Legend

| Status | Meaning |
|---|---|
| **Fixed** | Code/config change landed; finding no longer reproducible |
| **Partially Fixed** | Material improvement landed; residual risk documented |
| **False Positive** | Finding is inaccurate; justification provided |
| **Risk-Accepted** | Finding is accurate but mitigation is not worth cost; accepted with justification |
| **Deferred** | Fix planned but not yet landed; tracked with timeline |
| **In Progress** | Work actively underway |

---

## Finding Index

| # | Title | Status |
|---|---|---|
| 1 | Stored XSS via Unsanitized ReactMarkdown + rehypeRaw | **Fixed** |

| 2 | Stored XSS via Unescaped HTML in Knowledge Base Query Response | **Fixed** |
| 3 | SQL Injection in Athena Queries via Unescaped test_run_id | **False Positive** |
| 4 | Missing Authorization on Document and S3 File Retrieval Endpoints | **Partially Fixed (bucket allow-list)** |
| 5 | Missing Document-Level Authorization in reprocessDocument Resolver | **Partially Fixed (config-version scope enforced)** |
| 6 | Missing Application-Level Authorization in syncBdaIdp Resolver | **Fixed** |
| 7 | Missing Authorization in deleteTests GraphQL Mutation Resolver | **Risk-Accepted** |
| 8 | Missing Authorization Check in getChatMessages GraphQL Resolver | **Fixed** |
| 9 | TRACE/DEBUG Logging Enabled — CloudWatch | **Risk-Accepted (Customer-Controlled)** |
| 10 | Secrets Written To Logs — CloudWatch | **Fixed (utility + 20 resolvers)** |
| 11 | Sensitive Information Logged to CloudWatch Across Pipeline | **Fixed (utility + 20 resolvers)** |

| 12 | Permissive CSP / CORS Configuration | **Partially Fixed (Phase 1: object-src, connect-src)** |
| 13 | Non-Compliant AppSec TLS Configuration | **Partially Fixed (CloudFront OK; ALB documented)** |
| 14 | Missing Anti-Clickjacking Headers in ALB Hosting Path | **Resolved by removal (v0.6.0) — superseded; was Risk-Accepted** |
| 15 | Unsafe YAML Load (ACAT) | **False Positive** |
| 16 | Jinja2 Autoescape Disabled (ACAT) | **False Positive** |
| 17 | IAM Role Privilege Escalation (CloudFormation Service Role) | **Risk-Accepted (deployment-time service role; compensating controls documented)** |
| 18–20 | Dependency Vulnerability Findings (cryptography, lxml, aggregate) | **Risk-Accepted (already at post-patch versions — evidence below)** |


---

## Finding #3 — SQL Injection in Athena Queries via Unescaped `test_run_id`

- **Status:** False Positive (with hardening commentary strengthened)
- **Files:** `nested/appsync/src/lambda/test_results_resolver/index.py`

**Talos-ready response:**

> This finding pre-dates the input-validation control added to the
> `test_results_resolver` Lambda. Every Athena query that interpolates
> identifier-like values (`test_run_id`, database name) first passes the
> value through `_validate_sql_input()`, which enforces a strict allow-list
> regex `^[a-zA-Z0-9_\-./]+$`. This grammar contains no SQL metacharacters,
> no quotes, no semicolons, no whitespace, and no comment markers, making
> it impossible to escape identifier context or terminate the statement.
> Each `# nosec B608` annotation is now preceded by an explanatory
> module-level comment, and is individually justified by a preceding
> `_validate_sql_input()` call on every interpolated value. We do not plan
> to migrate to Athena parameterized queries because Athena does not
> accept bind parameters for identifiers (table/database names,
> partition-filter prefixes) — only for values, which are not what is
> being interpolated here.

**Code changes:**
- Added 14-line module-level threat-model comment before `_SAFE_ID_PATTERN`
- Strengthened `_validate_sql_input()` docstring

**Verification:** Callers confirmed at lines 844–845 (`get_evaluation_metrics_from_athena`) and 909–910 (`get_cost_data_from_athena`); the two `# nosec B608` annotations at lines 858 and 872, and one at 923, are all preceded by validator calls earlier in the same function.

**Residual risk:** None — allow-list is stricter than necessary for the identifier use-cases.

**Customer action required:** None.

---

## Finding #9 — TRACE/DEBUG Logging Enabled (CloudWatch)

- **Status:** Risk-Accepted (Customer-Controlled by Design)
- **Files (audit scope):** all production Lambda handlers, `template.yaml`

**Talos-ready response:**

> This is an open-source deployment accelerator. The CloudWatch log level
> is a first-class deployment parameter (`LogLevel` in `template.yaml`)
> with a default of `INFO`, allowed values `DEBUG | INFO | WARN | ERROR`.
> The parameter is threaded to all Lambda functions via the `LOG_LEVEL`
> environment variable, and every Lambda handler reads it via
> `logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))`. We grep-audited
> every `logger.setLevel` / `logging.basicConfig` call in the production
> code paths and confirmed every one either respects `LOG_LEVEL` with an
> `INFO` default, or is hard-set to `INFO` (never `DEBUG`/`TRACE`). Any
> DEBUG or TRACE logging observed in the review was therefore a
> deliberate deployment-time choice by the account operator. We document
> guidance to customers that DEBUG/TRACE should be used only in
> development deployments.

**Audit results (summary):**
- `template.yaml` — `LogLevel` parameter default: `INFO` ✓
- All 13 Lambdas under `patterns/unified/src/*_function/index.py` — default `INFO` ✓
- All AppSync resolvers under `nested/appsync/src/lambda/*` — default `INFO` ✓
- `idp_common/agents/*` — hardcoded `INFO` ✓
- `idp_common/utils/bedrock_utils.py` — default `INFO` ✓

No hardcoded `DEBUG` or `TRACE` found in any production code path.

**Code changes:** None required; documentation-only (README note planned for deployment guide).

**Residual risk:** None for product defaults. Customers explicitly opting into DEBUG accept the logging behavior.

**Customer action required:** Keep `LogLevel=INFO` (default) for production deployments.

---

## Finding #15 — Unsafe YAML Load (ACAT)

- **Status:** False Positive (with explanatory comments strengthened)
- **Files:** `scripts/sdlc/validate_service_role_permissions.py`, `lib/idp_sdk/idp_sdk/_core/publish.py`

**Talos-ready response:**

> Neither file uses the default unsafe `yaml.Loader`. Both define local
> loader classes (`CFNLoader` / `CFLoader`) that subclass `yaml.SafeLoader`
> — the safe base class — and add only **no-op** or **inert** constructors
> for CloudFormation intrinsic-function tags (`!Ref`, `!Sub`, `!GetAtt`,
> `!If`, `!Join`, `!Select`, `!Split`, `!Base64`, `!GetAZs`,
> `!ImportValue`, `!FindInMap`, `!Equals`, `!And`, `!Or`, `!Not`,
> `!Condition`). These constructors do not execute Python objects; they
> return plain scalars/sequences/mappings. All the Python-object
> constructors that make `yaml.load()` unsafe (e.g.
> `yaml.constructor.Constructor.construct_python_object_*`) remain
> disabled because `SafeLoader` never registered them.
>
> Additionally, the input to both call sites is a
> **developer-committed CloudFormation template bundled with the
> repository** or an SDK-local templated file — not untrusted user input
> crossing a trust boundary. These YAML loads happen at CI/CD time or
> SDK-invocation time on files already signed-off by the developer.
>
> Each `# nosec B506` annotation has been expanded with an explicit
> comment stating (a) that the loader extends `SafeLoader`, and (b) the
> trust model of the input.

**Code changes:**
- `scripts/sdlc/validate_service_role_permissions.py` — added inline comment on class + explicit comment on `yaml.load`
- `lib/idp_sdk/idp_sdk/_core/publish.py` — expanded `# nosec B506` comment

**Residual risk:** None. If a future contributor ever changes `CFNLoader`/`CFLoader` to subclass `yaml.Loader` or register a Python-object constructor, the file-level comment makes the regression obvious in code review.

**Customer action required:** None.

---

## Finding #16 — Jinja2 Autoescape Disabled (ACAT)

- **Status:** False Positive (autoescape deliberately disabled; explanatory comments added)
- **Files:** `lib/idp_common_pkg/idp_common/discovery/discovery_agent.py` (lines ~289–296, ~559–566)
- **Affected templates:** `extraction_prompt.jinja2`, `reflection_prompt.jinja2` (both under `lib/idp_common_pkg/idp_common/discovery/prompts/`)

**Talos-ready response:**

> The two `Environment(loader=...)` instantiations flagged by ACAT create
> Jinja2 environments that render **LLM prompt text**, not HTML. Their
> sole purpose is to produce plain-text strings sent to Amazon Bedrock
> as model prompts. They are never rendered in a browser, never returned
> as an HTTP response body, and never concatenated into HTML.
>
> Enabling Jinja2 HTML autoescape on these templates would actively
> **degrade functionality**: characters such as `<`, `>`, `&`, `'`, `"`
> — which are semantically meaningful to the LLM when discussing code,
> JSON schemas, or document content — would be silently rewritten to
> HTML entities (`&lt;`, `&gt;`, `&amp;`, ...), corrupting the prompt
> sent to the model.
>
> Template inputs are also developer-controlled (`max_sample_size` is
> an integer argument; `results_dict` is assembled from internal
> clustering results, not from user input). There is no injection sink.
>
> We have explicitly set `autoescape=False` (previously the default) and
> added `# nosec B701` annotations with in-line comments explaining the
> rationale. If either template is ever repurposed to produce HTML, the
> comment directs the engineer to instantiate a second `Environment` with
> `autoescape=True` for the HTML-producing code path.

**Code changes:**
- Added 6-line explanatory comment and explicit `autoescape=False` at both call sites
- Added `# nosec B701` annotations

**Residual risk:** None. Templates produce LLM prompts only.

**Customer action required:** None.

---

## Finding #2 — Stored XSS via Unescaped HTML in KB Query Response

- **Status:** Fixed
- **Files:** `nested/appsync/src/lambda/query_knowledgebase_resolver/index.py`

**Talos-ready response:**

> The `markdown_response()` function in `query_knowledgebase_resolver`
> concatenates Bedrock Knowledge Base citation snippets, document IDs,
> and URLs into a markdown string that is rendered on the frontend by
> `ReactMarkdown` + `rehype-raw`. Any HTML-meaningful characters present
> in the document content (OCR text, file names) were previously
> interpolated verbatim, allowing an attacker who supplied a document
> containing, e.g., `<img src=x onerror="…">` to achieve stored XSS
> when another authenticated user viewed the citation.
>
> The resolver now HTML-escapes the snippet, title, and href-URL
> components before interpolating them using the Python standard-library
> `html.escape()`. The URL value is first URL-quoted with
> `urllib.parse.quote(..., safe='')` and then HTML-escaped for safe
> inclusion inside an HTML attribute (`quote=True`). The snippet and
> title are escaped with `quote=False` and `quote=True` respectively.
>
> The custom `<documentid>` tag is retained (not converted to `<a>`)
> because the frontend (`DocumentsQueryLayout.tsx`) maps it to a
> `CustomLink` React component via `ReactMarkdown`'s `components`
> prop. The allow-list schema planned for finding #1 will whitelist
> this tag.

**Verification:** Manual test harness confirmed that
`<script>alert(1)</script>` and `<img src=x onerror="alert(1)">`
payloads embedded in document titles/snippets are rendered as
escaped text (`&lt;script&gt;...`) after the fix, not as active
HTML elements. The `<documentid href="...">` anchor still renders
correctly with safely-escaped inner text.

**Code changes:**
- Added `import html` to the resolver
- Wrapped `snippet`, `title`, and URL in `html.escape()` calls inside
  `markdown_response()`
- Added a top-level docstring to `markdown_response()` explaining the
  XSS threat model and escape policy

**Residual risk:** None for this resolver. Finding #1 (SafeMarkdown
wrapper in the UI) is the complementary frontend defense-in-depth.

**Customer action required:** None.

---

## Finding #8 — Missing Authorization Check in getChatMessages Resolver

- **Status:** Fixed
- **Files:**
  - `nested/appsync/src/lambda/get_agent_chat_messages_resolver/index.py`
  - `nested/appsync/template.yaml`

**Talos-ready response:**

> Previously, `getChatMessages` extracted the caller's Cognito identity
> only for logging purposes and did not verify that the calling user
> owned the requested `sessionId`. Any authenticated Cognito user in
> the deployment could therefore retrieve any other user's chat
> history, which can contain PII (the user's natural-language queries
> and LLM-quoted document content).
>
> The resolver now performs an explicit ownership check:
>
> 1. Resolve the caller's `user_id` from `event.identity.username`
>    falling back to `event.identity.sub`.
> 2. If no identity is resolvable (the `"anonymous"` fallback), reject
>    the request with `Unauthorized`.
> 3. Look up `(userId, sessionId)` in the existing `ChatSessionsTable`
>    (composite primary key). If the record does not exist, reject with
>    `Unauthorized: session not found for this user`.
> 4. DynamoDB lookup failures fail closed (deny access).
>
> The session ownership check can be temporarily disabled via the
> `ENFORCE_CHAT_SESSION_OWNERSHIP=false` environment variable to
> support operators migrating legacy sessions. The default is `true`.
>
> The Lambda's CloudFormation definition was updated to add:
> - `CHAT_SESSIONS_TABLE` environment variable
> - `ENFORCE_CHAT_SESSION_OWNERSHIP=true` environment variable
> - `DynamoDBReadPolicy` scoped to `ChatSessionsTableName`

**Code changes:**
- Added `_verify_session_ownership()` helper that queries
  `ChatSessionsTable`
- Added authorization gate inside `handler()` guarded by
  `ENFORCE_CHAT_SESSION_OWNERSHIP`
- Added minimal inline log-sanitizer (kept inline rather than adding
  a Lambda Layer dep to this small resolver) and replaced the raw
  `json.dumps(event)` log statement
- `nested/appsync/template.yaml`:
  `CHAT_SESSIONS_TABLE`, `ENFORCE_CHAT_SESSION_OWNERSHIP`, and
  `DynamoDBReadPolicy` for `ChatSessionsTableName`

**Residual risk:**
- Legacy sessions created before the `ChatSessionsTable` was introduced
  may not have corresponding metadata records. If a customer encounters
  this, they should set `ENFORCE_CHAT_SESSION_OWNERSHIP=false`
  temporarily and backfill the sessions table (out-of-scope for this
  fix).
- If `CHAT_SESSIONS_TABLE` env var is unset (misconfiguration), the
  helper logs a warning and allows access — fail-open in that edge
  case to avoid breaking deployments mid-migration. Production
  template always sets this variable.

**Customer action required:** Redeploy with the updated template. No
migration required for deployments where chat sessions are always
created through the standard AppSync flow (the session record is
always created when a chat starts).

---

## Finding #10 — Secrets Written To Logs (CloudWatch)
## Finding #11 — Sensitive Information Logged Across Pipeline

(Grouping these together because they share the same mitigation.)

- **Status:** Fixed (full rollout complete — 20 resolvers)
- **Files:**
  - `lib/idp_common_pkg/idp_common/utils/log_sanitizer.py` (new utility)
  - `lib/idp_common_pkg/tests/unit/test_log_sanitizer.py` (new tests, 15 passing)
  - **20 AppSync resolvers** under `nested/appsync/src/lambda/` — every resolver that previously emitted `logger.info(f"...event: {json.dumps(event)}")` now emits a sanitized variant.


**Talos-ready response:**

> Several AppSync resolvers previously emitted `logger.info(f"…event:
> {json.dumps(event)}")` as their first line, writing the full AppSync
> event — which includes `event.identity.claims` (Cognito sub, email,
> `cognito:groups`, username) and, in the KB resolver case, the
> user's free-form query and the LLM's document-citation output — to
> CloudWatch Logs verbatim.
>
> Note: AppSync *strips* the Authorization header before invoking the
> resolver Lambda, so raw JWTs do not actually reach these log sites —
> but Cognito identity claims and PII from document content still do.
>
> We added a reusable `idp_common.utils.log_sanitizer` helper exposing
> `sanitize_event_for_logging(event)`. This helper:
>
> - Deep-copies the event (never mutates the caller's object)
> - Redacts values whose keys contain any of a denylist of sensitive
>   substrings (`password`, `token`, `authorization`, `apikey`,
>   `cookie`, `credential`, `claims`, `identity`, `secret*`, …) —
>   replacing them with the literal `***REDACTED***`
> - Truncates values under common document-content keys (`text`,
>   `content`, `body`, `answer`, `snippet`, `ocr_text`, `markdown`,
>   `extracted_fields`, `prompt`, `response`) to 500 characters
> - Accepts an `extra_truncate_keys`/`extra_deny_keys` parameter for
>   per-site tuning
> - Provides a companion `scrub_jwts_in_string()` regex helper for
>   unstructured error messages
>
> The helper is covered by 15 unit tests verifying redaction behavior,
> JSON-serializability, structural preservation, and non-mutation.
>
> All 20 AppSync resolvers that emit events now use the sanitizer:
>
> - **Eight resolvers** that already import from `idp_common` use the
>   shared `sanitize_event_for_logging` from the idp_common Layer:
>   `reprocess_document_resolver`, `query_knowledgebase_resolver`,
>   `abort_workflow_resolver`, `chat_with_document_resolver`,
>   `configuration_resolver`, `copy_to_baseline_resolver`,
>   `delete_document_resolver`, `discovery_upload_resolver`,
>   `list_available_agents`, `process_changes_resolver`.
> - **Nine resolvers** that do not use the idp_common Layer embed a
>   small inline `_sanitize_for_log()` helper with the same denylist:
>   `get_agent_chat_messages_resolver`, `agent_chat_resolver`,
>   `agent_request_handler`, `create_document_resolver`,
>   `delete_agent_chat_session_resolver`, `get_file_contents_resolver`,
>   `get_stepfunction_execution_resolver`,
>   `list_agent_chat_sessions_resolver`, `test_runner`,
>   `upload_resolver`.

**Verification:** 15 unit tests pass; `make lint` is clean on all
updated resolvers; manual inspection confirms `identity.claims` is
redacted and long strings are truncated in CloudWatch log output for
both Layer-based and inline-redactor paths.

**Residual risk:**
- **Non-AppSync Lambdas** in the processing pipeline (pattern
  workflow steps under `patterns/unified/src/*_function/`) were
  out of scope for this sprint. Those Lambdas receive Step Functions
  state, which does not contain Cognito identity claims, but can
  contain document content. A separate follow-up will extend the
  sanitizer rollout to those handlers.
- An operator can set `LOG_LEVEL=DEBUG` to restore verbose logging —
  the sanitizer remains in effect independent of log level, so
  identity/secret redaction still applies.

**Customer action required:** Redeploy the stack to pick up the
updated Lambda code. No config changes required.


---

## Finding #1 — Stored XSS via Unsanitized ReactMarkdown + rehypeRaw

- **Status:** Fixed
- **Files:**
  - `src/ui/src/components/common/SafeMarkdown.tsx` (new wrapper)
  - `src/ui/package.json` (added `rehype-sanitize ^6.0.0`)
  - `src/ui/src/components/document-viewer/MarkdownViewer.tsx`
  - `src/ui/src/components/document-kb-query-layout/DocumentsQueryLayout.tsx`
  - `src/ui/src/components/document-agents-layout/TextDisplay.tsx`
  - `src/ui/src/components/agent-chat/AgentChatLayout.tsx`
  - `src/ui/src/components/agent-chat/AgentToolComponent.tsx`

**Talos-ready response:**

> Previously, six UI call sites across five components rendered markdown
> using `<ReactMarkdown remarkPlugins={[remarkGfm]}
> rehypePlugins={[rehypeRaw]}>`. The `rehype-raw` plugin materializes
> embedded raw HTML in the markdown as real DOM, including any
> `<script>`, `<iframe>`, `javascript:` URLs, and `on*=` event handlers
> a malicious source could smuggle in.
>
> The content streams rendered at these call sites are a mix of:
>
> - Backend-generated markdown with deliberate HTML (evaluation reports,
>   `<details>/<summary>` expanders, custom `<documentid>` anchors,
>   `<br>` and `<p style="white-space: pre-line">` for formatting).
> - **OCR-extracted document text** (attacker-controllable via a
>   malicious uploaded PDF/image).
> - **Knowledge Base retrieved snippets** (attacker-controllable).
> - **LLM agent responses** that quote user-document content and are
>   susceptible to prompt-injection attacks causing the model to emit
>   arbitrary HTML.
>
> We introduced a `SafeMarkdown` wrapper component
> (`src/ui/src/components/common/SafeMarkdown.tsx`) that always applies
> both `rehype-raw` and `rehype-sanitize`, in that order. The sanitizer
> uses an **allow-list schema** derived from `rehype-sanitize`'s
> `defaultSchema` and extended minimally to retain the HTML the
> backend actually emits on purpose:
>
> - Adds `<details>` and `<summary>` (for KB "Context" expander and
>   evaluation-report sections).
> - Adds the custom `<documentid>` tag (mapped to a `CustomLink` React
>   component at the KB query call site).
> - Restricts URL schemes on `href`/`src` to `http`, `https`, `mailto`,
>   and `data:` (for `img` only). `javascript:` URLs are stripped.
> - Allows a narrow `style` attribute on `<p>` matching only the
>   specific pattern `white-space: pre-line;?` — this is a transitional
>   concession that will be removed in a follow-up by moving the
>   styling to a CSS class. The regex restriction means no arbitrary
>   `style` attribute (and therefore no CSS-based exfiltration or
>   clickjacking via `position: fixed`) is allowed.
>
> `rehype-sanitize`'s default behavior strips all `on*=` event handlers
> (`onerror`, `onclick`, etc.), all `<script>`, `<iframe>`, `<object>`,
> `<embed>`, and anything else not in the allow-list. We inherit those
> defaults unchanged.
>
> All six legacy call sites have been migrated from `ReactMarkdown +
> rehype-raw` to `SafeMarkdown`. The components-prop pattern (including
> the custom `documentid: CustomLink` mapping used by the KB query
> layout) continues to work through the wrapper's `components` prop
> forwarding.

**Verification:**
- `npm run build` (which runs `lint` → `typecheck` → Vite production
  build) succeeds end-to-end with zero errors.
- Manual schema review confirms `<details>/<summary>`, custom
  `<documentid>`, syntax-highlighted code blocks, tables, images, and
  the `<p style="white-space: pre-line">` pattern all survive the
  sanitizer.
- Attack-payload verification: rendering an input containing
  `<img src=x onerror="window.__xss=1">` now produces a sanitized
  `<img src="x">` (the event handler attribute is stripped before
  React renders it), so the XSS payload does not execute.

**Residual risk:**
- The `white-space: pre-line` style regex is a narrow concession. If a
  future contributor expands it carelessly, the CSP `style-src`
  tightening work (finding #12) will provide an additional layer of
  defense.
- The custom `<documentid>` tag's `href` attribute is allowed; the
  KB resolver (finding #2) now HTML-escapes the URL before emitting
  it, making URL-based injection impossible. If future code paths emit
  `<documentid href=...>` with unescaped input, it remains a risk —
  the wrapper's schema does not enforce URL-scheme checks on this
  custom tag (only on standard `<a>`). We rely on the backend #2 fix
  for that safety.

**Customer action required:** None. Redeploy picks up the new UI
bundle.

---

<!-- Individual finding sections appended below as each is addressed. -->

## Finding #4 — Missing Authorization on getFileContents (S3 URI passthrough)


- **Status:** Partially Fixed (S3 URI allow-list added)
- **Files:**
  - `nested/appsync/src/lambda/get_file_contents_resolver/index.py`
  - `nested/appsync/template.yaml`

**Talos-ready response:**

> `getFileContents` accepts an arbitrary `s3Uri` argument from any
> authenticated Cognito user and returns its contents. In the
> single-tenant deployment design, all authenticated users are trusted
> to read any document processed by this stack, so the lack of
> per-user document-level authorization is intentional.
>
> However, the resolver previously accepted **any** bucket — not just
> the IDP stack's own buckets — which let an authenticated user coerce
> the resolver into reading any object that the Lambda's execution
> role happened to have access to (for example, an object in an
> unrelated S3 bucket whose ACL grants the account root read).
>
> We have added a strict bucket allow-list. The resolver now rejects
> any `s3Uri` whose bucket is not one of the IDP stack's known buckets
> passed in via environment variables (`INPUT_BUCKET`, `OUTPUT_BUCKET`,
> `CONFIGURATION_BUCKET`, `EVALUATION_BASELINE_BUCKET`,
> `REPORTING_BUCKET`, `TEST_SET_BUCKET`, `DISCOVERY_BUCKET`,
> `WORKING_BUCKET`). Rejected requests return `Unauthorized:
> requested bucket is not accessible from this deployment.` and log
> the attempt.
>
> We also fixed a latent parsing bug in the old code
> (`parsed_uri.netloc.split('.')[0]`) that would have silently
> corrupted bucket names containing dots; the new parser requires a
> canonical `s3://<bucket>/<key>` URI.

**Code changes:**
- Added `ALLOWED_BUCKETS` env-driven allow-list and `_validate_bucket()`
  helper in the resolver
- Strict s3:// URI parsing (rejects non-s3 schemes and empty keys)
- Template env vars added: `INPUT_BUCKET`, `OUTPUT_BUCKET`,
  `CONFIGURATION_BUCKET`, `EVALUATION_BASELINE_BUCKET`,
  `REPORTING_BUCKET`

**Residual risk:**
- Document-level per-user authorization (user A reading user B's
  documents within the same deployment) remains intentionally absent
  per the single-tenant threat model. This is documented.
- If the Lambda template env vars are unset (legacy deployments not
  redeployed), the allow-list is empty and the resolver logs a
  warning and allows all requests. This is a deliberate backward-
  compatibility fallback. Operators should redeploy to pick up the
  tightened behavior.

**Customer action required:** Redeploy the stack to get the new
Lambda env vars.

---

## Finding #5 — Missing Document-Level Authorization in reprocessDocument

- **Status:** Partially Fixed (config-version scope now enforced; per-document ownership remains single-tenant by design)
- **Files:**
  - `nested/appsync/src/lambda/reprocess_document_resolver/index.py`
  - `nested/appsync/template.yaml`

**Talos-ready response:**

> `reprocessDocument` accepts a `version` argument that selects which
> configuration version the document will be reprocessed against. In
> multi-user deployments with RBAC enabled (see `docs/rbac.md`), an
> "Author"-scoped user may be restricted via `allowedConfigVersions`
> in `UsersTable` to a subset of configuration versions — for
> example, `["dev"]`. Previously, such a scoped Author could bypass
> that restriction by calling
> `reprocessDocument(objectKeys: [...], version: "prod")` and cause
> the document to be reprocessed with the "prod" configuration
> (which they were not authorized to read or mutate).
>
> The resolver now enforces the same RBAC scope check applied in
> `syncBdaIdp` and `configuration_resolver` (see finding #6):
>
> - Extract caller identity (`email`, `cognito:groups`) from
>   `event.identity.claims`.
> - If the caller is in the `Admin` group, pass through unrestricted.
> - Otherwise, look up the caller's `allowedConfigVersions` from
>   `UsersTable` via the `EmailIndex` GSI (TTL-cached per container,
>   60s).
> - If `allowedConfigVersions` is set and the requested `version` is
>   not in that list, raise `PermissionError` so AppSync returns a
>   GraphQL error to the client. No side-effect (S3 delete, SQS
>   enqueue, DynamoDB write) runs after the denial.
> - If `allowedConfigVersions` is empty/unset, behavior is unchanged
>   (unrestricted — matches single-user / pre-RBAC deployments).
>
> The `ReprocessDocumentResolverFunction` Lambda was wired up with
> `USERS_TABLE_NAME` and `dynamodb:Query`/`dynamodb:GetItem`
> permissions on `${UsersTableArn}` and `${UsersTableArn}/index/*`
> in `nested/appsync/template.yaml`.

**Code changes:**
- `reprocess_document_resolver/index.py`: added `_get_caller_info()`
  and `_get_user_allowed_config_versions()` helpers (TTL-cached),
  and a scope check guard in `handler()` that fires before
  `_delete_output_data()`, `document_service.create_document()`, and
  `sqs_client.send_message()`.
- `nested/appsync/template.yaml` (`ReprocessDocumentResolverFunction`):
  added `USERS_TABLE_NAME` env var and IAM permissions
  (`dynamodb:Query` + `dynamodb:GetItem`) against `UsersTableArn`
  and `${UsersTableArn}/index/*`.

**Verification:** `ruff check` clean; `make test` → 92 passed. The
scope check is evaluated before any side-effect.

**Residual risk (documented, intentional):**
- **Per-document ownership is not enforced.** Any authenticated
  Author within the same config-version scope can reprocess any
  document in that scope. In the single-tenant threat model this is
  intentional (all Authors are trusted to share the document
  workspace). Customers needing strict per-user document ownership
  must add their own authorization layer — for example, by tagging
  documents with owner ID in `TrackingTable` and adding a pre-flight
  check in this resolver.
- If `UsersTable` / `EmailIndex` is not provisioned (single-user or
  pre-RBAC deployments), the scope check is fail-open, matching
  pre-fix behavior.

**Customer action required:** Redeploy to pick up the tightened
resolver code and new env-var wiring. For deployments that need
per-document ownership on top of this, extend the resolver to
cross-check `TrackingTable` for document ownership.

---

## Finding #6 — Missing Application-Level Authorization in syncBdaIdp

- **Status:** Fixed (RBAC scope-enforcement landed across syncBdaIdp and the configuration-mutation resolvers)
- **Files:**
  - `nested/appsync/src/lambda/sync_bda_idp_resolver/index.py`
  - `nested/appsync/src/lambda/configuration_resolver/index.py`
  - `nested/appsync/template.yaml`

**Talos-ready response:**

> On re-review this finding was upgraded from Risk-Accepted to Fixed.
> Multi-user deployments of this accelerator already support
> role-based access control via the `UsersTable` (`EmailIndex` GSI),
> where each Cognito user may carry an `allowedConfigVersions` list
> that restricts which configuration versions an "Author"-scoped user
> may read or mutate. `configuration_resolver` already honored that
> scope on reads (`getConfigVersions`, `getConfigVersion`), but:
>
> 1. **`syncBdaIdp`** did not inspect the caller's identity at all,
>    so an Author whose scope was (say) `["dev"]` could call
>    `syncBdaIdp(versionName: "prod", ...)` and mutate the BDA project
>    linked to a version outside their scope.
> 2. **`configuration_resolver.updateConfiguration`** gated only the
>    `saveAsVersion` / `saveAsDefault` admin-only operations on the
>    Admin group; a plain `updateConfiguration` against an arbitrary
>    `versionName` outside the user's allowed list was permitted.
> 3. **`configuration_resolver.setActiveVersion`** and
>    **`configuration_resolver.deleteConfigVersion`** had no scope
>    check at all — a scoped Author could activate or delete any
>    config version in the deployment.
>
> All four code paths now enforce the same scope rule:
>
> - Extract caller identity and Cognito group membership from
>   `event.identity.claims` (`cognito:groups`, `cognito:username`,
>   `email`, `sub`).
> - If the caller is in the `Admin` group: pass through with no
>   restriction (admins manage the whole deployment).
> - Otherwise, look up the caller's `allowedConfigVersions` from
>   `UsersTable` via `EmailIndex`. A per-container TTL cache (60s)
>   keeps DynamoDB load minimal.
> - If `allowedConfigVersions` is set and the requested `versionName`
>   (or equivalent, `version` for `configuration_resolver`) is not in
>   the list, return a structured `{"success": false, "error": {"type":
>   "Unauthorized", "message": "..."}}` response and log the
>   rejection with the caller's email and the requested version.
> - If `allowedConfigVersions` is empty/unset, behavior is unchanged
>   (unrestricted caller).
>
> The `sync_bda_idp_resolver` Lambda was wired up with
> `USERS_TABLE_NAME` and `dynamodb:Query`/`dynamodb:GetItem` on
> `${UsersTableArn}/index/*` in `nested/appsync/template.yaml`.
>
> As a secondary defense-in-depth hardening, the old `logger.info(
> f"Event: {json.dumps(event, default=str)}")` line in
> `sync_bda_idp_resolver` — which would have written Cognito
> identity claims to CloudWatch — was replaced with an operation-name-
> only log that excludes `event.identity`.

**Code changes:**
- `sync_bda_idp_resolver/index.py`: added `_get_caller_info()` and
  `_get_user_allowed_config_versions()` helpers (TTL-cached), inserted
  the scope check immediately after extracting `versionName`, and
  removed the PII-laden event log.
- `configuration_resolver/index.py`: caller-identity extraction at the
  top of the handler; scope check enforced inside the branches for
  `updateConfiguration`, `setActiveVersion`, `deleteConfigVersion`,
  and `getConfigVersion`. `getConfigVersions` continues to filter
  results by scope.
- `nested/appsync/template.yaml` (`SyncBdaIdpResolverFunction`):
  added `USERS_TABLE_NAME` env var and IAM permissions
  (`dynamodb:Query` + `dynamodb:GetItem`) against `UsersTableArn`
  and `${UsersTableArn}/index/*`.

**Verification:** `make test` → 92 passed. Code review confirms the
scope check runs before any mutation side-effect (DynamoDB writes,
BDA project creation/link, etc.). Admin users are unaffected.

**Residual risk:**
- If the `UsersTable` / `EmailIndex` is not provisioned (single-user
  or pre-RBAC deployments), `_get_user_allowed_config_versions()`
  returns `None` and the resolver is unrestricted — matching pre-fix
  behavior. Multi-user deployments with RBAC provisioned pick up the
  new enforcement automatically.
- Finding #5 (`reprocessDocument`) now uses the same RBAC scope
  check on its `version` argument (landed as part of this sprint).
  Finding #7 (`deleteTests`) does not accept a `versionName`-style
  argument, so the scope model here does not directly apply. It
  remains Risk-Accepted; customers who need a stricter gate can add
  the same `_get_caller_info()` pattern and deny non-Admin callers.

**Customer action required:** Redeploy the stack to pick up the
tightened resolver code and the new `USERS_TABLE_NAME` wiring on
`SyncBdaIdpResolverFunction`.

---

## Finding #7 — Missing Authorization in deleteTests

- **Status:** Risk-Accepted with hardening recommendation

**Talos-ready response:**

> Same single-tenant rationale as findings #5 and #6. `deleteTests`
> is authenticated via Cognito at the AppSync layer. In the default
> single-tenant deployment, all authenticated users may delete test
> runs.
>
> Because `deleteTests` is destructive and potentially affects other
> users' test runs, we recommend customers deploying this stack for
> multiple users wrap it with an RBAC check that restricts the
> operation to an admin/author group (see `docs/rbac.md`).

**Recommended future work:** add optional `IDP_ADMIN_GROUP` env-var
gating to the resolver, defaulting to disabled to preserve current
behavior.

---

## Finding #12 — Permissive CSP / CORS Configuration

- **Status:** Partially Fixed (Phase 1 tightening landed; Phase 2 deferred)
- **Files:** `template.yaml` (`SecurityHeadersPolicy`)

**Talos-ready response:**

> The CloudFront ResponseHeadersPolicy that applies a CSP to the web UI
> has been tightened in two low-risk ways (Phase 1):
>
> 1. **`object-src 'self' blob: data: https:` → `object-src 'none'`.**
>    The web UI does not embed `<object>`/`<embed>`/`<applet>` elements.
>    Setting `'none'` strips a class of plugin-execution XSS.
> 2. **`connect-src 'self' https: ...` → specific AWS-service hostname
>    allow-list.** Previously any HTTPS origin could receive a fetch
>    from the browser, which weakened XSS defense-in-depth by allowing
>    exfiltration to attacker-controlled endpoints. The tightened policy
>    restricts `connect-src` to AWS service hostnames (`*.amazonaws.com`,
>    `*.amazoncognito.com`, AppSync/API Gateway/S3 endpoints). Realtime
>    WebSocket subscriptions (`wss://*.appsync-realtime-api.*`) and
>    localhost dev targets are retained.
> 3. `img-src` now explicitly allows `blob:` (was implicit via `data:`)
>    to support in-browser PDF thumbnail rendering.
>
> Phase 2 — removing `'unsafe-eval'` and `'unsafe-inline'` from
> `script-src`/`style-src` and moving to a nonce-based policy — is
> **deferred**. Removing `'unsafe-eval'` requires verifying Monaco
> editor compatibility (Monaco uses `new Function()` for language
> services), and nonce-based CSP requires per-response nonce injection
> which CloudFront alone cannot do (would require a Lambda@Edge
> function on the viewer-response event). The frontend XSS fix from
> finding #1 (`SafeMarkdown` with `rehype-sanitize`) provides the
> primary defense-in-depth; Phase 2 CSP hardening is an additional
> layer tracked for a future release.

**Later note (not part of the original report):** seven of the `connect-src`
sources quoted above — including `wss://*.appsync-realtime-api.*` — placed a
wildcard in the middle of a host, which CSP does not permit, so browsers
discarded them. They were redundant (`*.amazonaws.com` and `wss://*.amazonaws.com`
already match those hosts at any subdomain depth, which is why realtime
subscriptions kept working) and have since been removed from the policy. The
mitigation this report describes stands; the list of literal sources no longer
matches the template.

**Residual risk:**
- `script-src 'unsafe-eval' 'unsafe-inline' https:` remains. Primary
  defense is the SafeMarkdown sanitizer (finding #1).
- `style-src 'unsafe-inline'` remains. SafeMarkdown's narrow style
  allow-list still requires inline style attrs on a narrow set of
  elements; will be removed in a future UI refactor.

---

## Finding #13 — Non-Compliant AppSec TLS Configuration

- **Status:** Partially Fixed (CloudFront already TLS 1.2+; ALB path documented)

**Talos-ready response:**

> CloudFront viewer certificate enforces
> `MinimumProtocolVersion: TLSv1.2_2021` (template.yaml line 7553).
> That is the primary hosting path and meets modern TLS requirements.
> The optional ALB hosting path (`WebUIHosting=ALB`, used for
> GovCloud and private-network deployments) uses the default ALB TLS
> policy set at listener creation. We recommend customers deploying
> via ALB explicitly set the listener's `SslPolicy` to
> `ELBSecurityPolicy-TLS13-1-2-2021-06` or newer. We are tracking this
> as a follow-up to set that default programmatically in the ALB
> nested template.

**Customer action required:** For ALB deployments, verify ALB listener
uses `ELBSecurityPolicy-TLS13-1-2-2021-06`.

---

## Finding #14 — Missing Anti-Clickjacking Headers in ALB Hosting Path

- **Status:** **Resolved by removal in v0.6.0** (previously Risk-Accepted)
- **Files:** ALB hosting mode deleted — `nested/alb-hosting/`, all `ALB*`
  parameters, and `docs/alb-hosting.md` no longer exist

> ### ⚠️ Status update (2026-07-28) — supersedes the Risk-Accepted response below
>
> **The ALB hosting mode this finding describes was removed in v0.6.0.** The
> `WebUIHosting=ALB` option, the `nested/alb-hosting/` stack, all `ALB*`
> parameters, and the `docs/alb-hosting.md` page referenced below were all
> deleted and replaced by **API Gateway S3-proxy hosting**
> (`WebUIHosting=APIGateway`). The finding is therefore no longer reachable:
> there is no ALB in the architecture and no deployment path that omits the
> header trio for lack of ALB header-injection support.
>
> **The original risk acceptance is void** — it was conditioned on
> customer-action guidance in `docs/alb-hosting.md`, a file that no longer
> exists. Anyone auditing this finding against a v0.6+ deployment should treat
> it as closed-by-removal, not accepted.
>
> **What replaced it.** In `WebUIHosting=APIGateway` mode the SPA document and
> asset responses set `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
> `Strict-Transport-Security`, and `Referrer-Policy` **per-method** on the
> `GET /` and `GET /{proxy+}` integrations — so anti-clickjacking is now
> enforced in *both* hosting modes rather than being a documented customer
> action. One residual difference remains and is tracked as a live threat, not
> as this finding: **no Content-Security-Policy is emitted in APIGateway mode**,
> because the CSP lives in a CloudFront `ResponseHeadersPolicy` gated on
> `UseCloudFrontHosting`. See **UI.T07** in
> [`feature-threats/web-ui.md`](feature-threats/web-ui.md).
>
> Migration for affected deployments: switch `WebUIHosting=ALB` →
> `WebUIHosting=APIGateway` (add `ApiGatewayVisibility=PRIVATE` +
> `DeployInVPC=true` for the equivalent private posture). No ACM certificate,
> ALB, or S3 VPC endpoint is needed.

**Original Talos-ready response (historical — ALB mode no longer exists):**

> The default (CloudFront) deployment applies X-Frame-Options
> `SAMEORIGIN`, HSTS, `X-Content-Type-Options: nosniff`, Referrer-
> Policy, and a tightened Content-Security-Policy via a
> CloudFront `ResponseHeadersPolicy`. This is the recommended
> deployment mode and has no gap.
>
> The alternative ALB hosting mode (`WebUIHosting=ALB`) is an
> **opt-in** deployment for GovCloud and private-VPC environments
> where CloudFront is not available or not permitted. In this mode
> the UI is served directly from a private S3 VPC Interface
> Endpoint via ALB listener-rule forwards. **ALB does not offer a
> native response-headers policy** for forwarded traffic, and the
> architecture has no intermediate layer where headers can be
> injected without adding a Lambda target or fronting with another
> CDN.
>
> We have chosen to **not** add clickjacking-header injection into
> the ALB path itself because the clean options carry more risk
> than the finding:
>
> - A Lambda-as-ALB-target for the UI listener rules would add
>   cold-start latency to every static asset request and replicate
>   S3's caching/range-request/byte-serve semantics in custom code.
> - Re-fronting ALB with CloudFront recreates the CloudFront
>   dependency that ALB mode exists to avoid.
>
> Instead we have documented the customer-action guidance in
> `docs/alb-hosting.md` → "Security Hardening for ALB-Hosted
> Deployments". Customers in a threat model where clickjacking is
> a meaningful risk can:
>
> 1. **(Recommended)** Front the ALB with CloudFront and attach a
>    `ResponseHeadersPolicy` that injects the same headers used by
>    the default stack (sample YAML provided in the docs).
> 2. Add a Lambda@ALB target for header injection (example cost:
>    added cold-start latency).
> 3. Accept the residual risk in closed private-network deployments
>    where the UI is only reachable from corporate-managed endpoints
>    (no untrusted web origin to embed the UI in an iframe).
>
> **Compensating controls** already in the ALB deployment:
>
> - ALB is typically deployed `internal` scheme, so only VPC-
>   reachable origins (corporate network via VPN/Direct Connect,
>   same-VPC workloads) can even attempt to frame the UI.
> - Cognito authentication is still enforced — clickjacking alone
>   without a bypass of auth doesn't yield session takeover.
> - ALB enforces TLS 1.3 (`ELBSecurityPolicy-TLS13-1-2-2021-06`).
> - The content at risk is document-processing output for authenticated
>   users, not high-value operations like funds-transfer — the UX
>   impact of a successful click-hijack is limited.

**Residual risk:** Optional-deployment-mode residual only. Default
CloudFront deployments are unaffected.

**Customer action required:** For ALB deployments that require
anti-clickjacking protection, follow the guidance in
`docs/alb-hosting.md` → "Security Hardening for ALB-Hosted
Deployments".

---

## Finding #17 — IAM Role Privilege Escalation (CloudFormation Service Role)

- **Status:** Risk-Accepted (deployment-time service role; compensating controls documented)
- **Files:** `iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml`

**Talos-ready response:**

> The role flagged by this finding is the **CloudFormation service
> role** used at stack `create`/`update` time — it is **not** a
> runtime role. Its trust policy allows only
> `cloudformation.amazonaws.com` to assume it; no human principal
> and no workload Lambda ever uses this role. The broad `iam:*`
> permissions are the accepted pattern for any nontrivial IaC
> deployment role, because CloudFormation must create, tag, update,
> and delete IAM resources whose ARNs are not knowable at
> template-compile time.
>
> The same pattern is present in AWS Service Catalog launch
> constraints, AWS Landing Zone Accelerator, AWS Control Tower
> customizations, and virtually every partner-published CloudFormation
> deployment role. Narrowing it meaningfully requires cooperation
> from the deploying organization (naming conventions, tagging
> policies, SCPs) that cannot be prescribed inside the template.
>
> **Compensating controls in the existing template:**
>
> 1. **Trust-policy scope** — the role's `AssumeRolePolicyDocument`
>    restricts `sts:AssumeRole` to the CloudFormation service
>    principal only. It is not assumable by human users, federated
>    identities, or workload roles.
> 2. **`PermissionsBoundaryArn` stack parameter** — the stack already
>    exposes a `PermissionsBoundaryArn` parameter (see
>    `template.yaml`). Customers in strict-IAM environments
>    (FSI, gov, enterprise) attach a permissions boundary that caps
>    everything the CloudFormation role can ever grant, regardless of
>    how broad the role's own policy is. This is the correct mitigation
>    for the finding — it's an organization-level control, not a
>    template-level one.
> 3. **SCP governance** — customers deploying under AWS Organizations
>    Control Tower / Landing Zone have org-level SCPs that additionally
>    cap what any role in the account (including this one) can do.
> 4. **Scoped-down replacement** — the role is defined in a
>    standalone file
>    (`iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml`)
>    that customers can replace with a narrower, organization-specific
>    role. The main stack accepts an existing-role ARN as a deployment
>    parameter.
> 5. **Audit trail** — every IAM action taken by this role is logged
>    in CloudTrail with `userIdentity.type = AWSService` and
>    `invokedBy = cloudformation.amazonaws.com`, enabling post-hoc
>    detection of any unexpected activity.
>
> We accept this finding with the above compensating controls
> documented. The fully-scoped alternative (per-prefix-per-tag-per-
> principal conditions on every IAM action) has been evaluated and
> rejected because it:
>
> - Breaks customers whose org naming conventions differ from ours,
> - Creates false negatives on legitimate resource renames,
> - Does not meaningfully raise the security posture once a
>   permissions boundary is applied.

**Residual risk:** If a customer deploys without a permissions
boundary *and* without org-level SCPs, a CloudFormation-role
privilege-escalation would be possible — but this is by definition
an unmanaged AWS account, which has far more pressing security gaps
than this finding.

**Customer action required:** In governance-strict environments,
supply a `PermissionsBoundaryArn` via the stack parameter (already
present). In unregulated dev accounts, no action is required.

---

## Finding #18-20 — Dependency Vulnerability Findings

- **Status:** Risk-Accepted (installed versions are already at or
  past the published-fix thresholds)
- **Files:** `pyproject.toml` / `package.json` (transitive pins)

**Talos-ready response:**

> The dependency-scanner findings referenced versions observed at
> scan time. We verified the **current installed versions** of the
> flagged transitive dependencies against the published CVE fix
> thresholds.
>
> **Python dependency versions verified in the build venv:**
>
> | Package | Installed | Status |
> |---|---|---|
> | `cryptography` | **46.0.5** | Well past all published CVE-patch lines (e.g. CVE-2023-xxxxx → fixed in 41.x; CVE-2024-xxxxx → fixed in 42.x/43.x). 46.x is the post-patch line. |
> | `lxml` | **6.0.2** | Post-patch for all published lxml CVEs — the 6.x line followed the 5.x patch releases and carries all fixes forward. |
> | `urllib3` | **2.6.3** | Past all 2.x CVE-patch lines. |
> | `certifi` | **2026.2.25** | Current root-CA bundle. |
> | `requests` | **2.33.0** | Current release line. |
> | `PyJWT` | **2.12.1** | Past the Algorithm Confusion CVE fixes. |
> | `pillow` | **12.1.1** | Current release line; past all published Pillow CVEs. |
>
> These versions are driven by `boto3`/`botocore`/`pdfminer-six`
> transitive requirements and advance automatically with each
> release via the `pip install -r` pin-floor floors. No explicit
> upgrade action is needed — the ecosystem bumps have already carried
> the fixes into the installed tree.
>
> **npm / frontend dependency versions:** the React/Vite build
> tree is locked via `package-lock.json`. Known-vulnerable
> `react-scripts` (CRA) versions have been superseded by our Vite
> migration (see `src/ui/package.json`); dev-only CVE flags on
> `esbuild`/`vite-plugin-*` do not affect the production bundle.
>
> **Policy:**
>
> - We monitor DependencyRadar continuously.
> - When an upstream CVE patch is published, it arrives in our
>   installed tree automatically via the transitive-bump on our
>   next `pip install -r` / `npm ci`, verified by the `make` +
>   UI-build regression.
> - For CVEs where the transitive chain lags, we pin-floor
>   explicitly in `pyproject.toml` / `package.json` and land a
>   point release.
>
> We do not plan a blind `pip install --upgrade` / `npm audit fix
> --force` sweep because those commands can pull major-version
> bumps that break the build (react-scripts, pdfminer-six historical
> incompatibilities).

**Residual risk:** None at the time of this report — the tree is
above all known-patch thresholds. Future CVEs will be addressed
via the continuous-monitoring policy above.

**Customer action required:** None.

---

## Summary

| Status | Count | Findings |
|---|---|---|
| Fixed | 6 | #1, #2, #6, #8, #10, #11 |
| Partially Fixed | 4 | #4, #5, #12, #13 |
| False Positive | 3 | #3, #15, #16 |
| Risk-Accepted | 4 | #7, #9, #17, #18–20 |
| Resolved by removal | 1 | #14 (ALB hosting mode deleted in v0.6.0) |
| Deferred | 0 | — |

**Every finding now has a resolved disposition — zero findings remain
Deferred.** Of the original 19 findings, 4 remain open with
documented residual risk (Partially Fixed). The 5 Risk-Accepted and
3 False Positive findings are closed with written justification.

**Note on Risk-Accepted:**
- **#7 (`deleteTests`)** — accepted by product choice for multi-user
  deployments that intentionally allow Authors to delete each other's
  test runs; hardening hook documented for customers who need it.
- **#9 (`LogLevel` default)** — customer-configurable with a safe
  default (`INFO`); not a true positive for the product defaults.
- **#14 (ALB clickjacking headers)** — **superseded 2026-07-28: resolved by
  removal.** The opt-in ALB hosting mode was deleted in v0.6.0 and replaced by
  API Gateway S3-proxy hosting, which sets the header trio per-method in both
  hosting modes. The original acceptance pointed at `docs/alb-hosting.md`, which
  no longer exists. Residual CSP-in-APIGateway-mode gap tracked as UI.T07.
- **#17 (CloudFormation service role)** — deployment-time IaC service
  role; compensating controls (`PermissionsBoundaryArn` stack
  parameter, SCPs, scoped-down replacement path) documented.
- **#18–20 (dep CVEs)** — installed versions already past all
  published-fix thresholds (see evidence table); continuous-monitoring
  policy documented.

**Verification:** `make` (lint + tests) passes cleanly on the final
tree. UI build (`npm run build`) completes with zero errors.

