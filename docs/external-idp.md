---
title: "External Identity Provider (Federation)"
---

# External Identity Provider (Federation)

This guide covers how to configure an external SAML or OIDC identity provider to federate authentication through Amazon Cognito for the GenAI IDP solution. Federated users sign in through their organization's identity provider and are automatically mapped to Cognito groups (Admin, Author, Reviewer, Viewer) based on group claims from the IdP.

> **Note — the `Annotator` role is not mappable from an external IdP.** There is no
> `ExternalIdPAnnotatorGroupName` parameter, so a federated user cannot be placed in
> the `Annotator` group by a group claim. Annotators must be created through User
> Management (or added to the Cognito group directly), which is also where their
> `allowedTestSets` scope is assigned — see [rbac.md](rbac.md). A federated user can
> still be added to `Annotator` manually after their first sign-in.

## Overview

The GenAI IDP solution supports optional federation with external identity providers including:

- **PingOne** (SAML or OIDC)
- **Okta** (SAML or OIDC)
- **Microsoft Entra ID (Azure AD)** (SAML or OIDC)

When federation is enabled:
1. Users see a "Sign in with [IdP Name]" button on the Cognito hosted UI
2. Clicking it redirects to the external IdP for authentication
3. After successful authentication, the IdP sends group claims back to Cognito
4. A post-authentication Lambda automatically maps IdP groups to Cognito groups
5. The user is redirected to the GenAI IDP web UI with appropriate permissions

Native Cognito username/password authentication remains available alongside federation.

## SAML vs OIDC — Which to Choose?

| Consideration | SAML | OIDC |
|---------------|------|------|
| Protocol maturity | Older, widely supported in enterprise | Modern, lightweight |
| Setup complexity | Slightly more involved (metadata XML, certificates) | Simpler (client ID/secret + issuer URL) |
| Group claims | Sent as SAML attributes in assertion | Sent as claims in ID token |
| Token format | XML-based assertions | JSON Web Tokens (JWT) |
| Best for | Enterprise SSO with existing SAML infrastructure | Modern apps, simpler integration |

Both work equally well with this solution. Choose based on what your IdP team prefers or what's already configured in your organization.

## CloudFormation Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `ExternalIdPType` | Yes | `SAML` or `OIDC` (leave empty to disable) |
| `ExternalIdPName` | Yes | Display name (e.g., `PingOne`, `Okta`) — alphanumeric and hyphens only |
| `ExternalIdPMetadataURL` | SAML only | SAML metadata document URL from your IdP |
| `ExternalIdPOIDCIssuer` | OIDC only | OIDC issuer URL (e.g., `https://auth.pingone.com/<env-id>/as`) |
| `ExternalIdPOIDCClientId` | OIDC only | OIDC client ID from your IdP |
| `ExternalIdPOIDCClientSecretArn` | OIDC only | ARN of a Secrets Manager secret containing the OIDC client secret (see [Storing the OIDC Client Secret](#storing-the-oidc-client-secret) below) |
| `ExternalIdPGroupAttributeName` | Optional | Attribute/claim name carrying group membership |
| `ExternalIdPAdminGroupName` | Optional | IdP group name that maps to Cognito Admin role |
| `ExternalIdPAuthorGroupName` | Optional | IdP group name that maps to Cognito Author role |
| `ExternalIdPReviewerGroupName` | Optional | IdP group name that maps to Cognito Reviewer role |
| `ExternalIdPViewerGroupName` | Optional | IdP group name that maps to Cognito Viewer role |
| `ExternalIdPAutoLogin` | Optional | `true` to auto-redirect to IdP, `false` (default) to show login page |

## Storing the OIDC Client Secret

For OIDC federation, the client secret must be stored in AWS Secrets Manager **before** deploying the stack. This ensures the secret never passes through CloudFormation parameters (which are visible via the `describe-stacks` API).

**Create the secret:**

```bash
aws secretsmanager create-secret \
  --name "my-idp-oidc-client-secret" \
  --description "OIDC client secret for GenAI IDP federation" \
  --secret-string "YOUR_CLIENT_SECRET_HERE" \
  --region us-east-1
```

Note the ARN from the output — you'll pass it as the `ExternalIdPOIDCClientSecretArn` parameter during deployment.

**To rotate the secret later:**

```bash
aws secretsmanager update-secret \
  --secret-id "my-idp-oidc-client-secret" \
  --secret-string "NEW_CLIENT_SECRET_HERE"
```

Then redeploy the stack to pick up the new value.

## Prerequisites (All Providers)

Before configuring any external IdP, you need:

1. A deployed GenAI IDP stack (deploy first with Cognito-only, then update with federation parameters)
2. From the stack outputs, note:
   - **User Pool ID** — e.g., `us-east-1_aBcDeFgHi`
   - **Cognito Domain** — e.g., `idp-1234567890.auth.us-east-1.amazoncognito.com`
3. Administrator access to your identity provider

The Cognito values you'll need for IdP configuration:

- **SAML ACS URL**: `https://<cognito-domain>/saml2/idpresponse`
- **SAML Entity ID**: `urn:amazon:cognito:sp:<user-pool-id>`
- **OIDC Redirect URI**: `https://<cognito-domain>/oauth2/idpresponse`

> **Custom domain in front of the Web UI?** The redirect URIs you register in the IdP are still the Cognito URLs above — they do **not** change when the application is fronted by a custom domain (e.g. `https://idp.your-org.com`). In this architecture Cognito brokers the federation, so the IdP only ever sees Cognito's `idpresponse` endpoint. The custom domain is configured separately via the `CustomDomainUrl` stack parameter, which adds it to the Cognito App Client's `CallbackURLs` and to all S3 bucket CORS origins. See [API Gateway Hosting → OAuth / Cognito callbacks](./apigateway-hosting.md#oauth--cognito-callbacks).

---

## PingOne

### Option A: PingOne SAML Setup

#### Step 1: Create a SAML Application in PingOne

1. Log in to the **PingOne Admin Console**
2. Navigate to **Connections** → **Applications**
3. Click **+ Add Application**
4. Select **Web App**, then choose **SAML**
5. Enter an application name (e.g., `GenAI IDP`)
6. Click **Configure**

#### Step 2: Configure SAML Settings

In the SAML configuration:

1. **ACS URL**:
   ```
   https://<cognito-domain>/saml2/idpresponse
   ```
2. **Entity ID**:
   ```
   urn:amazon:cognito:sp:<user-pool-id>
   ```
3. **Binding Type**: Select `HTTP POST`
4. **Signing**: Enable response signing
5. Click **Save**

#### Step 3: Configure Attribute Mappings

In the application's **Attribute Mappings** section, add:

| PingOne Attribute | SAML Attribute |
|-------------------|----------------|
| `Email Address` | `http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress` |
| `Group Names` | `http://schemas.xmlsoap.org/claims/Group` |

The Group Names attribute sends the user's PingOne group memberships in the SAML assertion.

#### Step 4: Create Groups and Assign Users

1. Navigate to **Identities** → **Groups**
2. Create groups that will map to GenAI IDP roles:
   - `IDP-Admins`
   - `IDP-Authors`
   - `IDP-Reviewers`
   - `IDP-Viewers`
3. Assign users to the appropriate groups
4. Ensure the application has access to these groups (check **Policies** if needed)

#### Step 5: Enable and Get Metadata URL

1. Toggle the application to **Enabled**
2. Go to **Configuration** → **Connection Details**
3. Copy the **SAML Metadata URL** — it looks like:
   ```
   https://auth.pingone.com/<environment-id>/saml20/metadata/<application-id>
   ```

#### Step 6: Deploy with SAML Parameters

```bash
idp-cli deploy \
    --stack-name my-idp-stack \
    --from-code . \
    --parameters "\
ExternalIdPType=SAML,\
ExternalIdPName=PingOne,\
ExternalIdPMetadataURL=https://auth.pingone.com/<env-id>/saml20/metadata/<app-id>,\
ExternalIdPGroupAttributeName=http://schemas.xmlsoap.org/claims/Group,\
ExternalIdPAdminGroupName=IDP-Admins,\
ExternalIdPAuthorGroupName=IDP-Authors,\
ExternalIdPReviewerGroupName=IDP-Reviewers,\
ExternalIdPViewerGroupName=IDP-Viewers" \
    --wait
```

---

### Option B: PingOne OIDC Setup

#### Step 1: Create an OIDC Application in PingOne

1. Log in to the **PingOne Admin Console**
2. Navigate to **Connections** → **Applications**
3. Click **+ Add Application**
4. Select **Web App**, then choose **OIDC**
5. Enter an application name (e.g., `GenAI IDP`)
6. Click **Configure**

#### Step 2: Configure OIDC Settings

1. **Grant Type**: Select `Authorization Code`
2. **Redirect URI**:
   ```
   https://<cognito-domain>/oauth2/idpresponse
   ```
3. **Token Endpoint Authentication Method**: `Client Secret Post` or `Client Secret Basic`
4. **Scopes**: Enable `openid`, `email`, `profile`
5. Click **Save**

#### Step 3: Add a Custom Groups Scope (if needed)

PingOne may not include group membership in tokens by default. To add it:

1. Go to the application's **Resources** tab
2. Under **Scopes**, add a custom scope or enable the `groups` scope
3. Alternatively, configure a custom claim:
   - Navigate to **Resources** → **Custom Attributes** or **Token Customization**
   - Add a claim named `groups` that maps to the user's group memberships

#### Step 4: Create Groups and Assign Users

Same as SAML Step 4 above — create `IDP-Admins`, `IDP-Authors`, `IDP-Reviewers`, `IDP-Viewers` groups and assign users.

#### Step 5: Enable and Get OIDC Details

1. Toggle the application to **Enabled**
2. From the application's **Configuration** tab, note:
   - **Client ID**
   - **Client Secret**
   - **Issuer URL** — typically:
     ```
     https://auth.pingone.com/<environment-id>/as
     ```
   You can verify the issuer by checking the well-known endpoint:
   ```
   https://auth.pingone.com/<environment-id>/as/.well-known/openid-configuration
   ```

#### Step 6: Deploy with OIDC Parameters

```bash
idp-cli deploy \
    --stack-name my-idp-stack \
    --from-code . \
    --parameters "\
ExternalIdPType=OIDC,\
ExternalIdPName=PingOne,\
ExternalIdPOIDCIssuer=https://auth.pingone.com/<env-id>/as,\
ExternalIdPOIDCClientId=<your-client-id>,\
ExternalIdPOIDCClientSecretArn=arn:aws:secretsmanager:us-east-1:123456789012:secret:my-pingone-oidc-secret-AbCdEf,\
ExternalIdPGroupAttributeName=groups,\
ExternalIdPAdminGroupName=IDP-Admins,\
ExternalIdPAuthorGroupName=IDP-Authors,\
ExternalIdPReviewerGroupName=IDP-Reviewers,\
ExternalIdPViewerGroupName=IDP-Viewers" \
    --wait
```

---

## Okta

### Option A: Okta SAML Setup

#### Step 1: Create a SAML Application in Okta

1. Log in to the **Okta Admin Console**
2. Navigate to **Applications** → **Applications**
3. Click **Create App Integration**
4. Select **SAML 2.0**, click **Next**
5. Enter an app name (e.g., `GenAI IDP`), click **Next**

#### Step 2: Configure SAML Settings

1. **Single sign-on URL**:
   ```
   https://<cognito-domain>/saml2/idpresponse
   ```
2. **Audience URI (SP Entity ID)**:
   ```
   urn:amazon:cognito:sp:<user-pool-id>
   ```
3. **Name ID format**: `EmailAddress`
4. **Application username**: `Email`

#### Step 3: Configure Attribute Statements

Add the following attribute statements in the **Attribute Statements** section:

| Name | Value |
|------|-------|
| `http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress` | `user.email` |
| `http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname` | `user.firstName` |
| `http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname` | `user.lastName` |

> **Note:** The `givenname` and `surname` attributes enable user display names in the IDP web portal. These standard SAML claim URIs work consistently across all identity providers (Okta, Entra ID, PingOne).

Add a **Group Attribute Statement**:

| Name | Filter |
|------|--------|
| `http://schemas.xmlsoap.org/claims/Group` | Matches regex: `IDP-.*` (or adjust to match your group naming) |

#### Step 4: Create Groups and Assign Users

1. Navigate to **Directory** → **Groups**
2. Create groups: `IDP-Admins`, `IDP-Authors`, `IDP-Reviewers`, `IDP-Viewers`
3. Assign users to groups
4. Go back to the application → **Assignments** tab → assign the groups to the app

#### Step 5: Get Metadata URL

1. Go to the application's **Sign On** tab
2. Under **SAML Signing Certificates**, find the **Metadata URL** — or use:
   ```
   https://<your-okta-domain>/app/<app-id>/sso/saml/metadata
   ```

#### Step 6: Deploy with SAML Parameters

```bash
idp-cli deploy \
    --stack-name my-idp-stack \
    --from-code . \
    --parameters "\
ExternalIdPType=SAML,\
ExternalIdPName=Okta,\
ExternalIdPMetadataURL=https://<okta-domain>/app/<app-id>/sso/saml/metadata,\
ExternalIdPGroupAttributeName=http://schemas.xmlsoap.org/claims/Group,\
ExternalIdPAdminGroupName=IDP-Admins,\
ExternalIdPAuthorGroupName=IDP-Authors,\
ExternalIdPReviewerGroupName=IDP-Reviewers,\
ExternalIdPViewerGroupName=IDP-Viewers" \
    --wait
```

---

### Option B: Okta OIDC Setup

#### Step 1: Create an OIDC Application in Okta

1. Log in to the **Okta Admin Console**
2. Navigate to **Applications** → **Applications**
3. Click **Create App Integration**
4. Select **OIDC - OpenID Connect**, then **Web Application**, click **Next**
5. Enter an app name (e.g., `GenAI IDP`)

#### Step 2: Configure OIDC Settings

1. **Grant type**: `Authorization Code`
2. **Sign-in redirect URI**:
   ```
   https://<cognito-domain>/oauth2/idpresponse
   ```
3. **Sign-out redirect URI**: Your web UI URL
4. **Assignments**: Select the groups or users who should have access

#### Step 3: Configure Groups Claim

1. Navigate to **Security** → **API** → **Authorization Servers**
2. Select the `default` authorization server (or your custom one)
3. Go to the **Claims** tab → **Add Claim**:
   - **Name**: `groups`
   - **Include in token type**: `ID Token` (Always)
   - **Value type**: `Groups`
   - **Filter**: Matches regex `IDP-.*` (or adjust to your naming)

#### Step 4: Get OIDC Details

From the application's **General** tab:
- **Client ID**
- **Client Secret**
- **Issuer URL**: `https://<your-okta-domain>/oauth2/default`

#### Step 5: Deploy with OIDC Parameters

```bash
idp-cli deploy \
    --stack-name my-idp-stack \
    --from-code . \
    --parameters "\
ExternalIdPType=OIDC,\
ExternalIdPName=Okta,\
ExternalIdPOIDCIssuer=https://<okta-domain>/oauth2/default,\
ExternalIdPOIDCClientId=<your-client-id>,\
ExternalIdPOIDCClientSecretArn=arn:aws:secretsmanager:us-east-1:123456789012:secret:my-okta-oidc-secret-AbCdEf,\
ExternalIdPGroupAttributeName=groups,\
ExternalIdPAdminGroupName=IDP-Admins,\
ExternalIdPAuthorGroupName=IDP-Authors,\
ExternalIdPReviewerGroupName=IDP-Reviewers,\
ExternalIdPViewerGroupName=IDP-Viewers" \
    --wait
```

---

## Microsoft Entra ID (Azure AD)

### Option A: Entra ID SAML Setup

#### Step 1: Create an Enterprise Application

1. Log in to the **Azure Portal**
2. Navigate to **Microsoft Entra ID** → **Enterprise applications**
3. Click **New application** → **Create your own application**
4. Enter a name (e.g., `GenAI IDP`), select **Integrate any other application**, click **Create**

#### Step 2: Configure SAML SSO

1. Go to **Single sign-on** → select **SAML**
2. Edit **Basic SAML Configuration**:
   - **Identifier (Entity ID)**:
     ```
     urn:amazon:cognito:sp:<user-pool-id>
     ```
   - **Reply URL (ACS URL)**:
     ```
     https://<cognito-domain>/saml2/idpresponse
     ```
3. Click **Save**

#### Step 3: Configure Attributes and Claims

1. Click **Edit** on **Attributes & Claims**
2. Verify the email claim exists:
   - **Claim name**: `http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress`
   - **Source attribute**: `user.mail`
3. Add a group claim:
   - Click **Add a group claim**
   - Select **Security groups** or **Groups assigned to the application**
   - Under **Advanced options**, check **Customize the name of the group claim**
   - **Name**: `http://schemas.xmlsoap.org/claims/Group`
   - **Source**: `Group ID` (or `Display Name` if you prefer readable names)

> **Note**: If using Group IDs, your `ExternalIdPAdminGroupName` etc. parameters must use the Entra group object IDs (GUIDs) rather than display names.

#### Step 4: Create Groups and Assign Users

1. Navigate to **Microsoft Entra ID** → **Groups**
2. Create groups: `IDP-Admins`, `IDP-Authors`, `IDP-Reviewers`, `IDP-Viewers`
3. Add users to groups
4. Go back to the Enterprise Application → **Users and groups** → assign the groups

#### Step 5: Get Metadata URL

1. Go to **Single sign-on** → **SAML Certificates** section
2. Copy the **App Federation Metadata Url** — it looks like:
   ```
   https://login.microsoftonline.com/<tenant-id>/federationmetadata/2007-06/federationmetadata.xml?appid=<app-id>
   ```

#### Step 6: Deploy with SAML Parameters

```bash
idp-cli deploy \
    --stack-name my-idp-stack \
    --from-code . \
    --parameters "\
ExternalIdPType=SAML,\
ExternalIdPName=EntraID,\
ExternalIdPMetadataURL=https://login.microsoftonline.com/<tenant-id>/federationmetadata/2007-06/federationmetadata.xml?appid=<app-id>,\
ExternalIdPGroupAttributeName=http://schemas.xmlsoap.org/claims/Group,\
ExternalIdPAdminGroupName=IDP-Admins,\
ExternalIdPAuthorGroupName=IDP-Authors,\
ExternalIdPReviewerGroupName=IDP-Reviewers,\
ExternalIdPViewerGroupName=IDP-Viewers" \
    --wait
```

---

### Option B: Entra ID OIDC Setup

#### Step 1: Register an Application

1. Log in to the **Azure Portal**
2. Navigate to **Microsoft Entra ID** → **App registrations**
3. Click **New registration**
4. Enter a name (e.g., `GenAI IDP`)
5. **Redirect URI**: Select `Web` and enter:
   ```
   https://<cognito-domain>/oauth2/idpresponse
   ```
6. Click **Register**

#### Step 2: Configure Client Secret

1. Go to **Certificates & secrets** → **New client secret**
2. Add a description and expiration
3. Copy the **Value** (this is your client secret — it's only shown once)

#### Step 3: Configure Token Claims

1. Go to **Token configuration** → **Add groups claim**
2. Select **Security groups** (or **Groups assigned to the application**)
3. For **ID token**, select **Group ID**
4. Click **Add**

#### Step 4: Configure API Permissions

1. Go to **API permissions**
2. Ensure these are granted:
   - `openid`
   - `email`
   - `profile`

#### Step 5: Get OIDC Details

- **Client ID**: From the app's **Overview** page
- **Client Secret**: From Step 2
- **Issuer URL**:
  ```
  https://login.microsoftonline.com/<tenant-id>/v2.0
  ```

#### Step 6: Deploy with OIDC Parameters

```bash
idp-cli deploy \
    --stack-name my-idp-stack \
    --from-code . \
    --parameters "\
ExternalIdPType=OIDC,\
ExternalIdPName=EntraID,\
ExternalIdPOIDCIssuer=https://login.microsoftonline.com/<tenant-id>/v2.0,\
ExternalIdPOIDCClientId=<your-client-id>,\
ExternalIdPOIDCClientSecretArn=arn:aws:secretsmanager:us-east-1:123456789012:secret:my-entraid-oidc-secret-AbCdEf,\
ExternalIdPGroupAttributeName=groups,\
ExternalIdPAdminGroupName=<admin-group-object-id>,\
ExternalIdPAuthorGroupName=<author-group-object-id>,\
ExternalIdPReviewerGroupName=<reviewer-group-object-id>,\
ExternalIdPViewerGroupName=<viewer-group-object-id>" \
    --wait
```

> **Note**: Entra ID OIDC sends group object IDs (GUIDs) by default, not display names. Use the group object IDs from the Azure Portal as your mapping values.

---

## Verifying the Integration

After deploying with federation parameters:

1. Navigate to the GenAI IDP web UI
2. The Cognito hosted UI should show a button labeled with your IdP name (e.g., "PingOne")
3. Click it to be redirected to your IdP for authentication
4. After signing in, you should be redirected back to the GenAI IDP web UI
5. Verify your role matches your IdP group membership by checking the user profile

## Removing Federation

To revert to Cognito-only authentication, update the stack and set `ExternalIdPType` back to empty:

```bash
idp-cli deploy \
    --stack-name my-idp-stack \
    --from-code . \
    --parameters "ExternalIdPType=" \
    --wait
```

This removes the external identity provider, the group mapping Lambda, and reverts the User Pool Client to Cognito-only. Existing Cognito-native users are unaffected.

## Troubleshooting

| Issue | Cause | Solution |
|-------|-------|----------|
| "Invalid saml_response" error | ACS URL or Entity ID mismatch | Verify they match exactly between your IdP and Cognito |
| User signs in but has no permissions | Group claim not mapped or group names don't match | Check `ExternalIdPGroupAttributeName` matches the IdP attribute name, and group names are case-sensitive exact matches |
| Redirect loop after sign-in | Callback URL mismatch | Ensure the Cognito User Pool Client callback URL includes your web UI URL (with and without trailing slash) |
| "Invalid_grant" error (OIDC) | Client secret incorrect or expired | Regenerate the client secret in your IdP and update the stack parameter |
| Groups not appearing in token | IdP not configured to send groups | Check IdP group claim configuration (see provider-specific steps above) |
| "Application is not enabled" (PingOne) | PingOne app is disabled | Toggle the application to Enabled in PingOne admin console |
| "Unsupported authentication method" (PingOne OIDC) | Token endpoint auth method mismatch | Set PingOne OIDC app to Client Secret Post |
| "RedirectUri is not registered" | Callback URL trailing slash mismatch | Ensure both with and without trailing slash variants are registered |
| User disabled but can still sign in | IdP SSO session still active in browser | Revoke the user's active sessions in the IdP admin console, or wait for the SSO session to expire |
| Sign out signs user back in (auto-login) | IdP SSO session persists after Cognito logout | Expected behavior with auto-login enabled — see Auto-Login Behavior below |
| `error_description=user.email%3A+Attribute+cannot+be+updated` after first login | User Pool was deployed before this fix with `email` schema `Mutable: false`. Cognito rewrites the mapped `email` attribute on every federated login; the second login is rejected. Schema flags are fixed at pool creation and cannot be changed in place. | Redeploy the stack against a new User Pool built from the current `template.yaml` (where `email` is `Mutable: true`). Existing federated users must re-federate against the new pool (no migration path for `EXTERNAL_PROVIDER` records). As a one-shot bridge, an admin can `aws cognito-idp admin-delete-user` the affected user — buys exactly one additional login. **Note:** any per-user state in DynamoDB keyed on the old Cognito `sub` (e.g., `UsersTable` preferences, `AllowedConfigVersions`) will be orphaned after the pool replacement — users will get fresh defaults. If continuity matters, an admin can scan the affected tables and re-key records from the old `sub` to the new one after re-federation. |

## Federated `email` Attribute Mutability

The User Pool `email` attribute is declared `Required: true, Mutable: true`. This is required for federation to work across multiple logins:

- Cognito's IdP attribute-mapping pipeline writes mapped attributes (`email`, `email_verified`, `family_name`, etc.) on **every** federated sign-in, not only on user-record creation.
- If `email` were `Mutable: false`, the second sign-in by any federated user (Okta, Entra ID, PingOne, generic SAML/OIDC) would fail with `user.email: Attribute cannot be updated` — see the AWS Cognito anti-pattern documented at <https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-settings-attributes.html>.
- `Required: true` is retained: every user record (native or federated) must have an email.
- The `UserPoolClient` does **not** include `email` in `WriteAttributes`, so authenticated end users cannot self-mutate their own email via the client SDK. Only the IdP mapping pipeline (federated logins) and admin APIs (`admin-update-user-attributes`) can write the field.
- Cognito-native sign-up and sign-in flows are unaffected by this setting — native users only write `email` once at account creation.

**Existing deployments:** schema flags are fixed at User Pool creation. Stacks deployed before this fix retain `Mutable: false` and will exhibit the lockout. The only durable remediation is to redeploy with a fresh User Pool; pre-existing `EXTERNAL_PROVIDER` records cannot be migrated and users must re-federate.

## Auto-Login Behavior

When `ExternalIdPAutoLogin` is enabled:

- Users are automatically redirected to the external IdP when visiting the app
- Sign-out clears the Cognito session and redirects through Cognito's logout endpoint
- If the IdP SSO session is still active, the user may be automatically signed back in on the next visit
- This is standard SSO behavior — the IdP controls session lifetime, not the app
- To fully sign out, users must also sign out of their IdP (e.g., PingOne, Okta, Entra ID)
- Organizations can control this by configuring SSO session timeouts in their IdP
- Set `ExternalIdPAutoLogin` to `false` if your organization requires explicit sign-in on each visit

### Checking Lambda Logs

The `ExternalIdPGroupMappingFunction` logs every federated sign-in with group mapping details. Check CloudWatch Logs for the function to debug group mapping issues:

```bash
aws logs tail /aws/lambda/<stack-name>-ExternalIdPGroupMappingFunction-<id> --follow
```
