"""Pre-token-generation Lambda trigger to map external IdP groups to Cognito groups.

The group claim arrives as the `custom:idp_groups` user-pool attribute, written by
Cognito's identity-provider AttributeMapping at each federated sign-in. That
attribute is `Mutable: true` and — when it is IdP-mapped — must also be listed in
the app client's `WriteAttributes` (Cognito writes mapped attributes *as the app
client*, and fails the sign-in if it lacks write access). A user's own access token
can therefore write it via `UpdateUserAttributes`.

Two checks keep a self-set value from granting groups:

1. **Provenance.** The user must be federated through the configured provider
   (`EXTERNAL_IDP_NAME`). Provenance comes from the Cognito-managed `identities`
   attribute read back via `AdminGetUser`, which no client can write. A native
   user who sets `custom:idp_groups` on themselves has no `identities` entry and
   is ignored.
2. **Freshness.** Only a fresh sign-in is honoured, never a token refresh. At a
   fresh federated sign-in Cognito has just rewritten the mapped attribute from
   the assertion, so the value read here is the IdP's. On a refresh the stored
   attribute may be whatever the user last wrote, so the mapping is skipped and
   the token carries the group membership already synced to Cognito.

Both checks fail closed: anything unverified returns the event untouched, which
leaves existing group membership alone and adds no token override.

Residual risk: a user who is *already* federated through the trusted provider can
self-set the attribute, and if their IdP later omits the group claim on a fresh
sign-in, Cognito may leave the self-set value in place for this trigger to read.
Assert the group claim for every federated user in the IdP to avoid that. See
docs/external-idp.md.
"""
import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

cognito = boto3.client("cognito-idp")

# Mapping: external IdP group name -> Cognito group name
GROUP_MAPPING = {}
for env_key, cognito_group in [
    ("ADMIN_GROUP_NAME", "Admin"),
    ("AUTHOR_GROUP_NAME", "Author"),
    ("REVIEWER_GROUP_NAME", "Reviewer"),
    ("VIEWER_GROUP_NAME", "Viewer"),
]:
    idp_group = os.environ.get(env_key, "").strip()
    if idp_group:
        GROUP_MAPPING[idp_group] = cognito_group

COGNITO_GROUPS = set(GROUP_MAPPING.values())

# Name of the Cognito identity provider whose assertions may grant groups. Set
# from the ExternalIdPName stack parameter. Empty means no provider is trusted,
# in which case no sign-in can be granted groups.
EXTERNAL_IDP_NAME = os.environ.get("EXTERNAL_IDP_NAME", "").strip()

# Trigger sources that represent a fresh sign-in, where Cognito has just applied
# the provider's AttributeMapping. TokenGeneration_RefreshTokens is deliberately
# absent: a refresh re-reads whatever is stored on the user, which the user may
# have written themselves. Anything not listed is treated as untrusted.
FRESH_SIGN_IN_TRIGGERS = frozenset(
    {
        "TokenGeneration_HostedAuth",
        "TokenGeneration_Authentication",
        "TokenGeneration_NewPasswordChallenge",
        "TokenGeneration_AuthenticateDevice",
    }
)


def parse_idp_groups(idp_groups_raw):
    """Parse IdP groups from various formats: JSON array, comma-separated, or single value."""
    if not idp_groups_raw or not idp_groups_raw.strip():
        return []

    idp_groups_raw = idp_groups_raw.strip()
    if idp_groups_raw.startswith("["):
        try:
            return json.loads(idp_groups_raw)
        except Exception:
            return [g.strip().strip('"') for g in idp_groups_raw.strip("[]").split(",")]
    else:
        return [g.strip() for g in idp_groups_raw.split(",")]


def is_federated_via_configured_idp(user_pool_id, username):
    """Return True only if `username` is linked to the configured IdP.

    Reads the Cognito-managed `identities` attribute through AdminGetUser rather
    than trusting the trigger event, so the answer comes from a field no client
    can write. Any failure — no configured provider, API error, unparseable
    value — returns False so the caller skips the mapping.
    """
    if not EXTERNAL_IDP_NAME:
        logger.warning(
            "EXTERNAL_IDP_NAME is not set; refusing to map groups for any sign-in"
        )
        return False

    try:
        response = cognito.admin_get_user(
            UserPoolId=user_pool_id, Username=username
        )
    except Exception as e:
        logger.error(f"Failed to read user {username} for provenance check: {e}")
        return False

    raw_identities = ""
    for attr in response.get("UserAttributes", []):
        if attr.get("Name") == "identities":
            raw_identities = attr.get("Value", "")
            break

    if not raw_identities:
        logger.warning(
            f"User {username} has no federated identities; ignoring custom:idp_groups"
        )
        return False

    try:
        identities = json.loads(raw_identities)
    except Exception as e:
        logger.error(f"Could not parse identities for user {username}: {e}")
        return False

    if not isinstance(identities, list):
        logger.error(f"Unexpected identities shape for user {username}")
        return False

    for identity in identities:
        if (
            isinstance(identity, dict)
            and identity.get("providerName") == EXTERNAL_IDP_NAME
        ):
            return True

    logger.warning(
        f"User {username} is not federated through '{EXTERNAL_IDP_NAME}'; "
        "ignoring custom:idp_groups"
    )
    return False


def handler(event, context):
    """Handle pre-token-generation trigger from Cognito."""
    trigger_source = event.get("triggerSource")
    logger.info(f"Pre-token trigger source: {trigger_source}")

    # Extract user pool ID from the event (avoids circular CloudFormation dependency)
    user_pool_id = event.get("userPoolId", "")
    username = event.get("userName", "")
    user_attributes = event.get("request", {}).get("userAttributes", {})
    idp_groups_raw = user_attributes.get("custom:idp_groups", "")

    if not idp_groups_raw:
        logger.info(f"No IdP groups claim for user {username}")
        return event

    # A refresh re-reads the stored attribute, which the user's own access token
    # may have written. Only a fresh sign-in has just had it rewritten from the
    # provider's assertion.
    if trigger_source not in FRESH_SIGN_IN_TRIGGERS:
        logger.info(
            f"Trigger source {trigger_source} is not a fresh sign-in; "
            f"leaving groups for user {username} unchanged"
        )
        return event

    if not is_federated_via_configured_idp(user_pool_id, username):
        return event

    idp_groups = parse_idp_groups(idp_groups_raw)
    logger.info(f"User {username} IdP groups: {idp_groups}")

    # Determine target Cognito groups from mapping
    target_groups = set()
    for idp_group in idp_groups:
        if idp_group in GROUP_MAPPING:
            target_groups.add(GROUP_MAPPING[idp_group])

    if not target_groups:
        logger.warning(f"No matching Cognito groups for user {username} with IdP groups {idp_groups}")
        return event

    # Get current Cognito groups for user
    try:
        response = cognito.admin_list_groups_for_user(
            UserPoolId=user_pool_id, Username=username
        )
        current_groups = {g["GroupName"] for g in response.get("Groups", [])}
    except Exception as e:
        logger.error(f"Failed to list groups for user {username}: {e}")
        return event

    # Add user to target groups they are not already in
    for group in target_groups - current_groups:
        try:
            cognito.admin_add_user_to_group(
                UserPoolId=user_pool_id, Username=username, GroupName=group
            )
            logger.info(f"Added user {username} to group {group}")
        except Exception as e:
            logger.error(f"Failed to add user {username} to group {group}: {e}")

    # Remove user from managed groups they should no longer be in
    for group in (current_groups & COGNITO_GROUPS) - target_groups:
        try:
            cognito.admin_remove_user_from_group(
                UserPoolId=user_pool_id, Username=username, GroupName=group
            )
            logger.info(f"Removed user {username} from group {group}")
        except Exception as e:
            logger.error(f"Failed to remove user {username} from group {group}: {e}")

    logger.info(f"User {username} group sync complete. Groups: {target_groups}")

    # Inject groups into the token so they are available immediately on first
    # sign-in, rather than only from the second token onwards.
    #
    # BOTH response keys are emitted because the key Cognito reads depends on the
    # pool's PreTokenGenerationConfig.LambdaVersion, and the two names are not
    # interchangeable:
    #   V1_0        -> claimsOverrideDetails
    #   V2_0/V3_0   -> claimsAndScopeOverrideDetails
    # The template registers the trigger with `PreTokenGeneration:`, which is
    # V1_0, so emitting only the V2 name meant the override was silently ignored
    # and a first sign-in produced a token with no group claim. Cognito ignores a
    # key it does not recognise, so writing both is safe and survives a later
    # move to V2_0/V3_0.
    group_override = {"groupOverrideDetails": {"groupsToOverride": list(target_groups)}}
    event.setdefault("response", {})
    event["response"]["claimsOverrideDetails"] = group_override
    event["response"]["claimsAndScopeOverrideDetails"] = group_override

    return event
