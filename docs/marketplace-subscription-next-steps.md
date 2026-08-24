---
title: "After Subscribing on AWS Marketplace"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# After subscribing on AWS Marketplace

**You're subscribed. There is no account to create here — the next step happens in
your own AWS account.**

Paid GenAI IDP extensions are installed into the **IDP Accelerator stack you
already run**, not into a service hosted by us. Your AWS account *is* your
identity: the extension proves who it is with the credentials of the Lambda
function running in your account, so there is no username, password, or API key to
set up. That is why this page has no sign-up form.

## Next steps

1. **Open your IDP Accelerator web console.**

   If you don't have the URL to hand, find it in CloudFormation:

   ```bash
   aws cloudformation describe-stacks \
     --stack-name <your-idp-stack-name> \
     --query "Stacks[0].Outputs[?OutputKey=='ApplicationWebURL'].OutputValue" \
     --output text
   ```

   Or open the [CloudFormation console](https://console.aws.amazon.com/cloudformation/home#/stacks),
   select your IDP stack, and look on the **Outputs** tab.

2. **Sign in as an administrator.** Installing an extension requires membership of
   the `Admin` Cognito group. If you aren't an admin, ask whoever administers the
   stack to complete the remaining steps.

3. **Go to the extension's page.** Use the left-hand navigation, or **Browse
   catalog** if the extension isn't listed there yet.

4. **Check the subscription banner.** Once AWS Marketplace has finished
   registering your subscription, the page shows **Subscription active**. It can
   take a minute or two to appear — reload the page if you still see *Subscription
   required*.

5. **Click Launch Stack.** This opens the CloudFormation console pre-filled with
   the extension's template and the parameters it needs from your IDP stack.
   Review them and choose **Create stack**. The extension registers itself back to
   your IDP console when it finishes, typically in 2–3 minutes.

That's it. The extension appears in your IDP console's navigation and is ready to
use.

## Don't have the IDP Accelerator deployed yet?

A paid extension has nothing to attach to on its own — it needs a host stack.
Deploy the accelerator first, then come back to step 1:

- [Quick Start](./quick-start.md) — fastest path to a working stack
- [Deployment guide](./deployment.md) — full options, including private networking

Your subscription keeps running in the meantime; nothing is wasted by deploying
the host afterwards.

## Troubleshooting

**The page still says "Subscription required".**
Marketplace subscriptions take a short time to propagate. Reload after a minute.
If it persists, confirm the subscription is **Active** in the
[AWS Marketplace subscriptions console](https://console.aws.amazon.com/marketplace/home#/subscriptions)
and that you are looking at the same AWS account you subscribed with — a
subscription held by another account in your organization is not visible to a
member account's stack.

**The page says "Access allowed without a verified subscription".**
The host couldn't confirm the subscription and is letting you through rather than
locking you out. The extension performs its own check at runtime, so it may still
decline to run. The banner names the cause; the usual ones are a missing
`aws-marketplace:SearchAgreements` permission on the host stack's role, or a stack
configured for simulator development. See
[Feature Platform](./feature-platform.md).

**"Not available in this Region".**
Extensions are published per Region, because a Lambda's code bucket must live in
the function's own Region. The banner lists the Regions the extension *is*
published to; the IDP Accelerator has to run in one of them.

**Launch Stack fails, or the extension installs but won't run.**
See [Feature Platform](./feature-platform.md) for the install flow and how
entitlement is checked, and the
[Feature Platform Developer Guide](./feature-platform-developer-guide.md) for how
an extension enforces its own subscription.

## Cancelling

Cancel from the
[AWS Marketplace subscriptions console](https://console.aws.amazon.com/marketplace/home#/subscriptions).
Cancelling the subscription does **not** delete the extension's CloudFormation
stack — delete that separately if you want its resources removed.
