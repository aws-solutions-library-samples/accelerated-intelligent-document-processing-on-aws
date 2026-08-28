# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Headless template transformation for IDP CloudFormation templates.

Transforms a standard IDP CloudFormation template into a headless version
by removing UI, the UI REST API, Cognito, WAF, Agent, HITL, and Knowledge
Base resources. The resulting template is suitable for API-only deployments
(e.g., GovCloud regions or headless use cases).
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional, Set

import yaml

logger = logging.getLogger(__name__)


class HeadlessTemplateTransformer:
    """Transform IDP CloudFormation templates for headless (no-UI) deployment.

    This class extracts and removes AWS services that are not needed for
    headless/API-only deployments: CloudFront, the UI REST API, Cognito, WAF,
    Agent/MCP, HITL/A2I, and Knowledge Base resources.

    Usage:
        transformer = HeadlessTemplateTransformer()
        success = transformer.transform(input_path, output_path)

        # Or transform in-memory:
        template = transformer.load_template(input_path)
        template = transformer.apply_transforms(template)
        transformer.save_template(template, output_path)
    """

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        if verbose:
            logging.basicConfig(
                level=logging.DEBUG, format="%(levelname)s: %(message)s"
            )
        else:
            logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

        # ---- Resource sets to remove ----

        self.ui_resources: Set[str] = {
            "CloudFrontDistribution",
            "CloudFrontOriginAccessControl",
            "SecurityHeadersPolicy",
            "WebUIBucket",
            "WebUIBucketPolicy",
            "UICodeBuildProject",
            "UICodeBuildServiceRole",
            "StartUICodeBuild",
            "StartUICodeBuildExecutionRole",
            "StartUICodeBuildLogGroup",
            "CodeBuildRun",
        }

        # UI-API resources to strip for headless. AWS AppSync has been removed;
        # the interactive UI API is now an API Gateway REST API hosted in the
        # APIRESOLVERSTACK nested stack (its dispatcher invokes these resolver
        # Lambdas). Headless = no UI API, so strip the nested stack and the
        # UI-only resolver Lambdas it fronts. (The former AppSync
        # DataSource/Resolver/GraphQLApi resources no longer exist in the base
        # template, so they are not listed here anymore.)
        self.appsync_resources: Set[str] = {
            # Nested stack hosting the REST API dispatcher + UI resolver Lambdas.
            "APIRESOLVERSTACK",
            # UI-only resolver Lambdas defined in the main template (capacity
            # planning, version check, fine-tuning) — only reachable via the
            # (stripped) UI API, so remove them in headless mode.
            "CalculateCapacityResolverFunction",
            "CalculateCapacityResolverFunctionLogGroup",
            "VersionCheckResolverFunction",
            "VersionCheckResolverFunctionLogGroup",
            # API-Gateway Web UI hosting (WebUIHosting=APIGateway). These only
            # exist to serve the Web UI as an S3 proxy on the UI REST API and to
            # register its Cognito OAuth callback URLs — meaningless in headless
            # mode, and they reference removed resources (APIRESOLVERSTACK,
            # UserPool, UserPoolClient, WebUIBucket), so strip them.
            "WebUIProxyRole",
            "WebUIClientOAuthUrls",
            "WebUIClientOAuthUrlsFunction",
            "WebUIClientOAuthUrlsRole",
            "WebUIClientOAuthUrlsLogGroup",
        }

        self.auth_resources: Set[str] = {
            "UserPool",
            "UserPoolClient",
            "UserPoolDomain",
            "IdentityPool",
            "CognitoIdentityPoolSetRole",
            "CognitoAuthorizedRole",
            "AdminUser",
            "AdminGroup",
            "AuthorGroup",
            "ViewerGroup",
            "ReviewerGroup",
            "AnnotatorGroup",
            "AdminUserToGroupAttachment",
            "GetDomain",
            "CognitoUserPoolEmailDomainVerifyFunction",
            "CognitoUserPoolEmailDomainVerifyFunctionLogGroup",
            "CognitoUserPoolEmailDomainVerifyPermission",
            "CognitoUserPoolEmailDomainVerifyPermissionReady",
            # External Identity Provider (SAML/OIDC) — depends on UserPool
            "ExternalIdentityProvider",
            "ExternalIdentityProviderReady",
            "ExternalIdPGroupMappingFunction",
            "ExternalIdPGroupMappingFunctionLogGroup",
            "ExternalIdPGroupMappingCognitoPolicy",
            "ExternalIdPGroupMappingPermission",
        }

        self.waf_resources: Set[str] = {
            "WAFIPV4Set",
            "WAFLambdaServiceIPSet",
            "WAFWebACL",
            "WAFWebACLAssociation",
            "IPSetUpdaterFunction",
            "IPSetUpdaterCustomResource",
        }

        self.agent_resources: Set[str] = {
            "AgentTable",
            "AgentRequestHandlerFunction",
            "AgentRequestHandlerLogGroup",
            "AgentProcessorFunction",
            "AgentProcessorLogGroup",
            "ExternalMCPAgentsSecret",
            "ListAvailableAgentsFunction",
            "ListAvailableAgentsLogGroup",
            "AgentCoreAnalyticsLambdaFunction",
            "AgentCoreAnalyticsLambdaLogGroup",
            "AgentCoreGatewayManagerFunction",
            "AgentCoreGatewayManagerLogGroup",
            "AgentCoreGatewayExecutionRole",
            "AgentCoreGateway",
            "ExternalAppClient",
            "AgentCoreMCPHandlerFunction",
            "AgentCoreMCPHandlerLogGroup",
            "MCPConnectorClient",
            "MCPResourceServer",
        }

        self.hitl_resources: Set[str] = {
            "UserPoolClienta2i",
            "PrivateWorkteam",
            "CognitoClientUpdaterRole",
            "CognitoClientUpdaterFunctionLogGroup",
            "CognitoClientUpdaterFunction",
            "CognitoClientCustomResource",
            "A2IFlowDefinitionRole",
            "A2IHumanTaskUILambdaRole",
            "CreateA2IResourcesLambda",
            "A2IResourcesCustomResource",
            "GetWorkforceURLFunction",
            "WorkforceURLResource",
            "UsersTable",
            "UserManagementFunctionLogGroup",
            "UserManagementFunction",
            "UserSyncFunctionLogGroup",
            "UserSyncFunction",
            "CompleteSectionReviewFunctionLogGroup",
            "CompleteSectionReviewFunction",
            "UserManagementDataSource",
            "CreateUserResolver",
            "DeleteUserResolver",
            "ListUsersResolver",
            "CompleteSectionReviewDataSource",
            "CompleteSectionReviewResolver",
            "SkipAllSectionsReviewResolver",
            "ClaimReviewResolver",
            "ReleaseReviewResolver",
            "GetMyProfileResolver",
            "UpdateUserResolver",
        }

        self.kb_resources: Set[str] = {
            "DOCUMENTKB",
        }

        # Feature Platform nested stack — gated on EnableFeaturePlatform=true.
        # Its parameters wire in the UI REST API (in APIRESOLVERSTACK), UserPool,
        # UserPoolClient, WebUIBucket, and DiscoveryBucket — all removed in
        # headless mode — and it DependsOn the (removed) APIRESOLVERSTACK. Left in
        # place, its references to those removed resources would fail at deploy
        # time ("Fn::GetAtt references undefined resource"). EnableFeaturePlatform
        # is forced to 'false' in _remove_parameters so the
        # IsFeaturePlatformDisabled-gated TrackingTableName export stays active.
        self.feature_platform_resources: Set[str] = {
            "FeaturePlatformStack",
        }

        self.discovery_resources: Set[str] = {
            "DiscoveryBucket",
            "DiscoveryBucketPolicy",
            "DiscoveryDLQ",
            "DiscoveryQueue",
            "DiscoveryTrackingTable",
            "DiscoveryProcessorFunction",
            "DiscoveryProcessorFunctionLogGroup",
            "BlueprintOptimizationFunction",
            "BlueprintOptimizationFunctionLogGroup",
            "MultiDocDiscoveryPrepareFunction",
            "MultiDocDiscoveryPrepareFunctionLogGroup",
            "MultiDocDiscoveryStateMachine",
            "MultiDocDiscoveryStateMachineRole",
            "MULTIDOCDISCOVERYSTACK",
        }

        self.appsync_dependent_resources: Set[str] = {
            "StepFunctionSubscriptionPublisher",
            "StepFunctionSubscriptionPublisherLogGroup",
            "StepFunctionSubscriptionRule",
            "StepFunctionSubscriptionPublisherPermission",
            # Invoked async only by the UI REST API dispatcher in the (removed)
            # APIRESOLVERSTACK; with no caller, the function and its log group are
            # dead weight in headless mode and their dangling refs to removed
            # UI resources (e.g. UsersTable) would block stack updates.
            "ChatWithDocumentProcessorFunction",
            "ChatWithDocumentProcessorLogGroup",
            # Chat streaming endpoint (Function URL + LWA). UI-only: reached
            # directly by the browser via VITE_STREAM_URL (emitted only by the
            # removed UI CodeBuild/WebUITestEnvFile), invoked by the removed
            # CognitoAuthorizedRole, and it references the removed UsersTable
            # (RBAC scope) — so a dangling "Fn::GetAtt UsersTable" would block
            # headless deploys. Strip the whole family.
            "ChatStreamProcessorFunction",
            "ChatStreamProcessorLogGroup",
            "ChatStreamProcessorUrl",
            "ChatStreamProcessorUrlPermission",
        }

        # ---- Parameters to remove ----

        self.parameters_to_remove: Set[str] = {
            "AdminEmail",
            "AllowedSignUpEmailDomain",
            "CloudFrontPriceClass",
            "CloudFrontAllowedGeos",
            "WAFAllowedIPv4Ranges",
            "DocumentKnowledgeBase",
            "KnowledgeBaseVectorStore",
            "KnowledgeBaseModelId",
            "ChatCompanionModelId",
            "EnableHITL",
            "ExistingPrivateWorkforceArn",
            # External Identity Provider (SAML/OIDC) — depends on Cognito
            "ExternalIdPType",
            "ExternalIdPName",
            "ExternalIdPMetadataURL",
            "ExternalIdPOIDCIssuer",
            "ExternalIdPOIDCClientId",
            "ExternalIdPOIDCClientSecretArn",
            "ExternalIdPGroupAttributeName",
            "ExternalIdPAdminGroupName",
            "ExternalIdPAuthorGroupName",
            "ExternalIdPReviewerGroupName",
            "ExternalIdPViewerGroupName",
            "ExternalIdPAutoLogin",
            # Public-template version-check (UI-only feature — resolver
            # removed above in self.appsync_resources). Drop the params so
            # the headless template doesn't expose unused UI configuration.
            "PublicArtifactsBucket",
            "PublicArtifactsPrefix",
        }

        # ---- Outputs to remove ----

        self.outputs_to_remove: Set[str] = {
            "ApplicationWebURL",
            "WebUIBucketName",
            "WebUITestEnvFile",
            "SageMakerA2IReviewPortalURL",
            "LabelingConsoleURL",
            "ExternalMCPAgentsSecretName",
            "PrivateWorkteamArn",
            "MCPServerEndpoint",
            "MCPClientId",
            "MCPClientSecret",
            "MCPUserPool",
            "MCPTokenURL",
            "MCPAuthorizationURL",
            "DynamoDBAgentTableName",
            "DynamoDBAgentTableConsoleURL",
            "ExternalMCPAgentsSecretConsoleURL",
            "MCPConnectorClientId",
            "MCPConnectorClientSecret",
            "S3DiscoveryBucketName",
            "S3DiscoveryBucketConsoleURL",
            # UI-API DNS helper output (named for the former AppSync endpoint).
            # AppSync is gone — the UI API is now an API Gateway REST API in the
            # (stripped) APIRESOLVERSTACK — so this UI-only output has no place in a
            # headless deployment. Kept here as a defensive strip in case the
            # base template still emits it.
            "AppSyncEndpointForDNS",
        }

        # ---- Conditions to remove ----

        self.conditions_to_remove: Set[str] = {
            "ShouldAllowSignUpEmailDomain",
            "ShouldEnableGeoRestriction",
            "IsWafEnabled",
            "ShouldCreateDocumentKnowledgeBase",
            "ShouldUseDocumentKnowledgeBase",
            "IsHITLEnabled",
            "ShouldCreatePrivateWorkteam",
            "ShouldUseExistingPrivateWorkteam",
            # External Identity Provider (SAML/OIDC) — depends on Cognito
            "HasExternalIdP",
            "IsExternalIdPSAML",
            "IsExternalIdPOIDC",
            "HasExternalIdPGroupMapping",
            "ShouldMapExternalIdPGroups",
            "CreateExternalAppClient",
            # Used only by the removed VersionCheckResolverFunction (AppSync UI feature)
            "HasPublicArtifactsBucket",
            # Tests the AdminEmail parameter against the CI sentinel to suppress
            # the Cognito invite email. AdminEmail is removed above, and this
            # condition's only consumer was AdminUser's MessageAction (also
            # removed) — but a Condition left referencing a deleted parameter is a
            # HARD template error, not dead weight:
            #   Template format error: Unresolved dependencies [AdminEmail].
            #   Cannot reference resources in the Conditions block of the template
            # CloudFormation rejects the whole template at validate/create time,
            # so every headless deploy failed before creating a single resource.
            "SuppressAdminInvite",
        }

        # ---- Rules to remove ----

        self.rules_to_remove: Set[str] = {
            "ValidateExistingPrivateWorkforceArn",
        }

        # ---- Parameter groups to remove ----

        self.parameter_groups_to_remove: Set[str] = {
            "User Authentication",
            "Security Configuration",
            "Document Knowledge Base",
            "Agentic Analysis",
            "HITL (A2I) Configuration",
            "External Identity Provider",
        }

    @property
    def all_resources_to_remove(self) -> Set[str]:
        """Combined set of all resources that should be removed."""
        return (
            self.ui_resources
            | self.appsync_resources
            | self.appsync_dependent_resources
            | self.auth_resources
            | self.waf_resources
            | self.agent_resources
            | self.hitl_resources
            | self.kb_resources
            | self.discovery_resources
            | self.feature_platform_resources
        )

    # ---- Public API ----

    def transform(
        self,
        input_path: str,
        output_path: str,
        *,
        update_govcloud_config: bool = False,
    ) -> bool:
        """Transform a template file to headless and save to output_path.

        Args:
            input_path: Path to the source CloudFormation YAML template.
            output_path: Path to write the headless template.
            update_govcloud_config: If True, update configuration maps and
                defaults for GovCloud-specific config presets.

        Returns:
            True if transformation and basic validation succeeded.
        """
        try:
            logger.info("🔧 Starting headless template transformation")
            template = self.load_template(input_path)
            template = self.apply_transforms(
                template, update_govcloud_config=update_govcloud_config
            )
            self.save_template(template, output_path)

            if not self.validate_template_basic(template):
                return False

            logger.info("🎉 Headless template transformation completed successfully!")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to generate headless template: {e}")
            if self.verbose:
                import traceback

                logger.debug(traceback.format_exc())
            return False

    def apply_transforms(
        self,
        template: Dict[str, Any],
        *,
        update_govcloud_config: bool = False,
    ) -> Dict[str, Any]:
        """Apply all headless transformations to an in-memory template dict.

        Args:
            template: Parsed CloudFormation template dictionary.
            update_govcloud_config: If True, update config maps for GovCloud.

        Returns:
            The transformed template dictionary.
        """
        template = self._remove_resources(template)
        template = self._remove_parameters(template)
        template = self._remove_outputs(template)
        template = self._remove_conditions(template)
        template = self._remove_rules(template)
        template = self._clean_for_headless_deployment(template)
        template = self._clean_parameter_groups(template)
        if update_govcloud_config:
            template = self._update_configuration_maps_for_govcloud(template)
        template = self._update_arn_partitions(template)
        template = self._update_description(template)
        return template

    # ---- I/O helpers ----

    def load_template(self, input_file: str) -> Dict[str, Any]:
        """Load CloudFormation template from YAML file."""
        logger.info(f"Loading template from {input_file}")
        if not os.path.exists(input_file):
            raise FileNotFoundError(f"Input template file not found: {input_file}")
        try:
            with open(input_file, "r", encoding="utf-8") as f:
                template = yaml.safe_load(f)
            logger.debug(
                f"Loaded template with {len(template.get('Resources', {}))} resources"
            )
            return template
        except yaml.YAMLError as e:
            raise ValueError(f"Failed to parse YAML template: {e}")

    def save_template(self, template: Dict[str, Any], output_file: str) -> None:
        """Save CloudFormation template to YAML file."""
        logger.info(f"Saving headless template to {output_file}")
        os.makedirs(
            os.path.dirname(output_file) if os.path.dirname(output_file) else ".",
            exist_ok=True,
        )
        try:
            with open(output_file, "w", encoding="utf-8") as f:
                yaml.dump(template, f, default_flow_style=False, width=120, indent=2)
            logger.info("✅ Headless template saved successfully")
        except Exception as e:
            raise ValueError(f"Failed to save template: {e}")

    # ---- Validation ----

    def validate_template_basic(self, template: Dict[str, Any]) -> bool:
        """Perform basic template validation."""
        logger.info("Validating generated template")
        issues: List[str] = []

        required_sections = ["AWSTemplateFormatVersion", "Resources"]
        for section in required_sections:
            if section not in template:
                issues.append(f"Missing required section: {section}")

        resources = template.get("Resources", {})
        core_resources = {
            "InputBucket",
            "OutputBucket",
            "WorkingBucket",
            "TrackingTable",
            "ConfigurationTable",
            "CustomerManagedEncryptionKey",
        }
        missing_core = core_resources - set(resources.keys())
        if missing_core:
            issues.append(f"Missing core resources: {', '.join(missing_core)}")

        if "PATTERNSTACK" not in resources:
            issues.append(
                "Missing PATTERNSTACK - unified pattern nested stack should be present"
            )

        if issues:
            logger.error("Basic template validation failed:")
            for issue in issues:
                logger.error(f"  - {issue}")
            return False

        logger.info("✅ Basic template validation passed")
        return True

    # ---- Internal transformation methods ----

    def _remove_resources(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """Remove resources not supported in headless deployment."""
        resources = template.get("Resources", {})
        original_count = len(resources)

        removed_resources: List[str] = []
        for resource_name in list(resources.keys()):
            resource_def = resources[resource_name]

            if resource_name in self.all_resources_to_remove:
                del resources[resource_name]
                removed_resources.append(resource_name)
                continue

            # Remove if resource depends on a condition that we're removing
            if isinstance(resource_def, dict) and "Condition" in resource_def:
                condition_name = resource_def["Condition"]
                if condition_name in self.conditions_to_remove:
                    del resources[resource_name]
                    removed_resources.append(
                        f"{resource_name} (depends on removed condition: {condition_name})"
                    )

        logger.info(
            f"Removed {len(removed_resources)} resources ({original_count} → {len(resources)})"
        )
        logger.debug(f"Removed resources: {', '.join(removed_resources)}")
        return template

    def _remove_parameters(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """Remove parameters related to unsupported services."""
        parameters = template.get("Parameters", {})
        original_count = len(parameters)

        removed: List[str] = []
        for param_name in list(parameters.keys()):
            if param_name in self.parameters_to_remove:
                del parameters[param_name]
                removed.append(param_name)

        # Set EnableMCP default to false (MCP depends on Cognito/AgentCore)
        if "EnableMCP" in parameters:
            parameters["EnableMCP"]["Default"] = "false"
            logger.info("Modified EnableMCP parameter default to 'false'")

        # Force Feature Platform off — its nested stack (removed above) wires
        # in AppSync/Cognito/WebUI/Discovery resources that don't exist in
        # headless mode. Setting the default to 'false' keeps
        # IsFeaturePlatformDisabled true so the main stack's
        # TrackingTableName export (gated on that condition) stays active.
        if "EnableFeaturePlatform" in parameters:
            parameters["EnableFeaturePlatform"]["Default"] = "false"
            logger.info("Modified EnableFeaturePlatform parameter default to 'false'")

        logger.info(
            f"Removed {len(removed)} parameters ({original_count} → {len(parameters)})"
        )
        return template

    def _remove_outputs(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """Remove outputs related to unsupported services."""
        outputs = template.get("Outputs", {})
        original_count = len(outputs)

        removed: List[str] = []
        for output_name in list(outputs.keys()):
            if output_name in self.outputs_to_remove:
                del outputs[output_name]
                removed.append(output_name)

        logger.info(
            f"Removed {len(removed)} outputs ({original_count} → {len(outputs)})"
        )
        return template

    def _remove_conditions(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """Remove conditions related to unsupported services."""
        conditions = template.get("Conditions", {})
        original_count = len(conditions)

        # Force IsS3VectorsVectorStore to always false (S3 Vectors not available headless)
        if "IsS3VectorsVectorStore" in conditions:
            conditions["IsS3VectorsVectorStore"] = {"Fn::Equals": ["false", "true"]}
            logger.info("Forced IsS3VectorsVectorStore to always False")

        removed: List[str] = []
        for condition_name in list(conditions.keys()):
            if condition_name in self.conditions_to_remove:
                del conditions[condition_name]
                removed.append(condition_name)

        if removed:
            logger.info(
                f"Removed {len(removed)} conditions ({original_count} → {len(conditions)})"
            )
        return template

    def _remove_rules(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """Remove rules that validate removed parameters."""
        rules = template.get("Rules", {})
        if not rules:
            return template

        original_count = len(rules)
        removed: List[str] = []
        for rule_name in list(rules.keys()):
            if rule_name in self.rules_to_remove:
                del rules[rule_name]
                removed.append(rule_name)

        if removed:
            logger.info(
                f"Removed {len(removed)} rules ({original_count} → {len(rules)})"
            )

        if not rules:
            del template["Rules"]
            logger.debug("Removed empty Rules section")

        return template

    def _clean_for_headless_deployment(
        self, template: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Clean template for headless deployment.

        - Remove CloudFront policy statements
        - Remove CORS from S3 buckets
        - Strip dangling references to removed resources from kept Lambda
          functions (e.g. ExternalMCPAgentsSecret policy statements)
        - Clean nested stack parameters
        - Fix Knowledge Base condition references
        - Clean UpdateSettingsValues custom resource
        - Clean outputs referencing removed resources
        """
        logger.info("Cleaning template for headless deployment")
        resources = template.get("Resources", {})

        # Remove CloudFront policy statements
        template = self._clean_cloudfront_policy_statements(template)

        # Remove CORS from all S3 buckets (no web UI)
        for resource_name, resource_def in resources.items():
            if (
                isinstance(resource_def, dict)
                and resource_def.get("Type") == "AWS::S3::Bucket"
            ):
                properties = resource_def.get("Properties", {})
                if "CorsConfiguration" in properties:
                    del properties["CorsConfiguration"]
                    logger.debug(f"Removed CORS configuration from {resource_name}")

        # Tidy backend functions that survive in headless mode. The base
        # template already tracks via DynamoDB (DOCUMENT_TRACKING_MODE=dynamodb,
        # APPSYNC_API_URL=""), so there is no AppSync→DynamoDB conversion to do.
        # What still matters: drop the now-empty APPSYNC_API_URL env var and
        # strip any policy statements that reference resources we removed (e.g.
        # the ExternalMCPAgentsSecret on the agent-chat function, or a stray
        # appsync:GraphQL action) so no dangling refs remain.
        functions_to_clean = [
            "QueueSender",
            "QueueProcessor",
            "WorkflowTracker",
            "EvaluationFunction",
            "AgentChatProcessorFunction",
            "AgentProcessorFunction",
            "DiscoveryProcessorFunction",
            # The circuit-breaker manager still does real work in headless mode
            # (state management, alarm processing); we keep it but drop the
            # empty APPSYNC_API_URL env var and any appsync:GraphQL policy.
            "CircuitBreakerManagerFunction",
        ]
        for func_name in functions_to_clean:
            if func_name in resources:
                self._clean_tracking_function(resources, func_name)

        # Clean nested stack parameters (PATTERNSTACK)
        for stack_name in ["PATTERNSTACK"]:
            if stack_name in resources:
                self._clean_nested_stack_params(resources, stack_name)

        # Fix ShouldUseDocumentKnowledgeBase condition references
        template = self._fix_kb_condition_references(template)

        # Re-fetch resources after potential yaml reload
        resources = template.get("Resources", {})

        # Clean outputs referencing removed resources
        self._clean_orphaned_outputs(template)

        # Fix UpdateSettingsValues custom resource
        self._clean_update_settings_values(template)

        return template

    def _clean_tracking_function(
        self, resources: Dict[str, Any], func_name: str
    ) -> None:
        """Tidy a kept Lambda function for headless deployment.

        The base template already tracks documents via DynamoDB directly
        (DOCUMENT_TRACKING_MODE=dynamodb, TRACKING_TABLE set, APPSYNC_API_URL=""),
        so this is no longer an AppSync→DynamoDB conversion. It just drops the
        now-empty APPSYNC_API_URL env var, ensures a DynamoDB CRUD policy is
        present, and strips any policy statements that reference removed
        resources (ExternalMCPAgentsSecret) or stale appsync:GraphQL actions.
        """
        func_def = resources[func_name]
        env_vars = (
            func_def.get("Properties", {}).get("Environment", {}).get("Variables", {})
        )

        # Drop the leftover (empty) APPSYNC_API_URL env var. The base template
        # already sets DOCUMENT_TRACKING_MODE=dynamodb and TRACKING_TABLE, so we
        # only backfill them defensively if they are somehow missing.
        if "APPSYNC_API_URL" in env_vars:
            del env_vars["APPSYNC_API_URL"]
            env_vars.setdefault("DOCUMENT_TRACKING_MODE", "dynamodb")
            env_vars.setdefault("TRACKING_TABLE", {"Ref": "TrackingTable"})
            logger.debug(f"Removed empty APPSYNC_API_URL env var from {func_name}")

        # Add DynamoDB CRUD policy if not present
        policies = func_def.get("Properties", {}).get("Policies", [])
        has_dynamodb = any(
            isinstance(p, dict) and "DynamoDBCrudPolicy" in p for p in policies
        )
        if not has_dynamodb:
            policies.append(
                {"DynamoDBCrudPolicy": {"TableName": {"Ref": "TrackingTable"}}}
            )
            logger.debug(f"Added DynamoDB CRUD permissions for {func_name}")

        # Clean AppSync and MCP-related policy statements
        cleaned_policies: List[Any] = []
        for policy in policies:
            if isinstance(policy, dict) and "Statement" in policy:
                cleaned = self._clean_policy_statements(policy, func_name)
                if cleaned is not None:
                    cleaned_policies.append(cleaned)
            else:
                cleaned_policies.append(policy)
        func_def["Properties"]["Policies"] = cleaned_policies

    def _clean_policy_statements(
        self, policy: Dict[str, Any], func_name: str
    ) -> Optional[Dict[str, Any]]:
        """Clean AppSync/MCP policy statements. Returns None if policy should be removed entirely."""
        statement = policy["Statement"]

        if isinstance(statement, dict):
            if self._should_remove_statement(statement):
                logger.debug(f"Removed AppSync/MCP policy statement from {func_name}")
                return None
            return policy

        if isinstance(statement, list):
            cleaned = [
                s
                for s in statement
                if isinstance(s, dict) and not self._should_remove_statement(s)
            ]
            if not cleaned:
                logger.debug(f"Removed AppSync/MCP permissions from {func_name} policy")
                return None
            policy["Statement"] = cleaned
            return policy

        return policy

    def _should_remove_statement(self, stmt: Dict[str, Any]) -> bool:
        """Check if a policy statement references AppSync or removed resources."""
        action = stmt.get("Action")
        resource = stmt.get("Resource")

        # Remove if action contains appsync:GraphQL
        if isinstance(action, str) and "appsync:GraphQL" in action:
            return True
        if isinstance(action, list) and any(
            "appsync:GraphQL" in str(a) for a in action
        ):
            return True

        # Remove if references ExternalMCPAgentsSecret (removed resource)
        if self._references_removed_resource(resource, "ExternalMCPAgentsSecret"):
            return True

        return False

    def _references_removed_resource(self, resource: Any, ref_name: str) -> bool:
        """Check if a resource reference points to a removed resource."""
        if isinstance(resource, dict) and resource.get("Ref") == ref_name:
            return True
        if isinstance(resource, list):
            return any(
                isinstance(r, dict) and r.get("Ref") == ref_name for r in resource
            )
        return False

    def _clean_nested_stack_params(
        self, resources: Dict[str, Any], stack_name: str
    ) -> None:
        """Clean nested stack parameters for headless deployment."""
        stack_params = resources[stack_name].get("Properties", {}).get("Parameters", {})

        if "EnableHITL" in stack_params:
            stack_params["EnableHITL"] = "false"
            logger.debug(f"Hardcoded EnableHITL to false in {stack_name}")

        if "SageMakerA2IReviewPortalURL" in stack_params:
            stack_params["SageMakerA2IReviewPortalURL"] = '""'
            logger.debug(
                f"Hardcoded SageMakerA2IReviewPortalURL to empty in {stack_name}"
            )

        if "AppSyncApiUrl" in stack_params:
            del stack_params["AppSyncApiUrl"]
            logger.debug(f"Removed AppSyncApiUrl parameter from {stack_name}")

        if "AppSyncApiArn" in stack_params:
            del stack_params["AppSyncApiArn"]
            logger.debug(f"Removed AppSyncApiArn parameter from {stack_name}")

        # Discovery-related parameters (resources removed in headless)
        # The nested stack declares Default: "" for DiscoveryBucket and
        # DiscoveryTrackingTable, so we can simply delete the parameters
        # and let CloudFormation use the defaults.
        for param in [
            "DiscoveryBucket",
            "DiscoveryTrackingTable",
            "DiscoveryBucketName",
            "DiscoveryTrackingTableName",
            "MultiDocDiscoveryStateMachineArn",
        ]:
            if param in stack_params:
                del stack_params[param]
                logger.debug(f"Removed {param} parameter from {stack_name}")

        # Remove dependencies on GraphQLApi
        stack_deps = resources[stack_name].get("DependsOn", [])
        if isinstance(stack_deps, list) and "GraphQLApi" in stack_deps:
            resources[stack_name]["DependsOn"] = [
                dep for dep in stack_deps if dep != "GraphQLApi"
            ]
            logger.debug(f"Removed GraphQLApi dependency from {stack_name}")
        elif stack_deps == "GraphQLApi":
            del resources[stack_name]["DependsOn"]
            logger.debug(f"Removed GraphQLApi dependency from {stack_name}")

    def _fix_kb_condition_references(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """Fix ShouldUseDocumentKnowledgeBase condition references."""
        template_str = yaml.dump(template, default_flow_style=False)
        if "ShouldUseDocumentKnowledgeBase" in template_str:
            template_str = re.sub(
                r"ShouldUseDocumentKnowledgeBase:\s*\n\s*Fn::If:\s*\n\s*-\s*ShouldUseDocumentKnowledgeBase\s*\n\s*-\s*true\s*\n\s*-\s*false",
                "ShouldUseDocumentKnowledgeBase: false",
                template_str,
                flags=re.MULTILINE,
            )
            template = yaml.safe_load(template_str)
            logger.warning(
                "⚠️  Fixed ShouldUseDocumentKnowledgeBase condition reference"
            )
            logger.warning(
                "   Note: Knowledge Base functionality disabled for headless deployment"
            )
        return template

    def _clean_orphaned_outputs(self, template: Dict[str, Any]) -> None:
        """Remove outputs that reference removed resources."""
        outputs = template.get("Outputs", {})
        outputs_to_clean: List[str] = []

        for output_name, output_def in outputs.items():
            if isinstance(output_def, dict):
                output_value = output_def.get("Value", {})
                if isinstance(output_value, dict):
                    if output_value.get("Ref") in ("AgentTable", "WebUIBucket"):
                        outputs_to_clean.append(output_name)
                    elif "AgentTable" in str(output_value):
                        outputs_to_clean.append(output_name)

        for output_name in outputs_to_clean:
            if output_name in outputs:
                del outputs[output_name]
                logger.debug(
                    f"Removed output {output_name} (references removed resource)"
                )

    def _clean_update_settings_values(self, template: Dict[str, Any]) -> None:
        """Fix UpdateSettingsValues custom resource — remove references to removed resources."""
        resources = template.get("Resources", {})
        if "UpdateSettingsValues" not in resources:
            return

        update_settings = resources["UpdateSettingsValues"]
        settings_kvp = update_settings.get("Properties", {}).get(
            "SettingsKeyValuePairs", {}
        )

        if "KnowledgeBaseId" in settings_kvp:
            settings_kvp["KnowledgeBaseId"] = ""
            logger.debug("Replaced KnowledgeBaseId with empty string")

        if "ShouldUseDocumentKnowledgeBase" in settings_kvp:
            settings_kvp["ShouldUseDocumentKnowledgeBase"] = False
            logger.debug("Set ShouldUseDocumentKnowledgeBase to False")

        if "AllowedSignUpEmailDomains" in settings_kvp:
            del settings_kvp["AllowedSignUpEmailDomains"]
            logger.debug("Removed AllowedSignUpEmailDomains from UpdateSettingsValues")

        # Remove Discovery-related settings (resources removed in headless)
        # plus the public-template version-check settings (UI-only feature
        # whose resolver and parameters are removed in headless mode).
        for key in [
            "DiscoveryBucket",
            "PublicArtifactsBucket",
            "PublicArtifactsPrefix",
        ]:
            if key in settings_kvp:
                del settings_kvp[key]
                logger.debug(f"Removed {key} from UpdateSettingsValues")

    def _clean_cloudfront_policy_statements(
        self, template: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Remove CloudFront-related policy statements from remaining resources."""
        logger.info("Removing CloudFront policy statements from remaining resources")
        resources = template.get("Resources", {})

        for resource_name, resource_def in resources.items():
            if not isinstance(resource_def, dict):
                continue

            if resource_def.get("Type") == "AWS::S3::BucketPolicy":
                policy_doc = resource_def.get("Properties", {}).get(
                    "PolicyDocument", {}
                )
                self._clean_policy_document_cloudfront(policy_doc, resource_name)

            elif resource_def.get("Type") == "AWS::IAM::Role":
                policies = resource_def.get("Properties", {}).get("Policies", [])
                for policy in policies:
                    if isinstance(policy, dict) and "PolicyDocument" in policy:
                        policy_name = policy.get("PolicyName", "unnamed")
                        self._clean_policy_document_cloudfront(
                            policy["PolicyDocument"],
                            f"{resource_name}.{policy_name}",
                        )

        return template

    def _clean_policy_document_cloudfront(
        self, policy_doc: Dict[str, Any], resource_identifier: str
    ) -> None:
        """Clean CloudFront-related statements from a policy document."""
        if not isinstance(policy_doc, dict) or "Statement" not in policy_doc:
            return

        statements = policy_doc["Statement"]
        if not isinstance(statements, list):
            return

        cleaned_statements: List[Any] = []
        for statement in statements:
            if not isinstance(statement, dict):
                cleaned_statements.append(statement)
                continue

            principal = statement.get("Principal", {})
            should_remove = False

            if isinstance(principal, dict):
                service = principal.get("Service")
                if isinstance(service, str) and "cloudfront." in service.lower():
                    should_remove = True
                elif isinstance(service, dict) and "Fn::Sub" in service:
                    fn_sub_value = service["Fn::Sub"]
                    if (
                        isinstance(fn_sub_value, str)
                        and "cloudfront." in fn_sub_value.lower()
                    ):
                        should_remove = True
                elif isinstance(service, list):
                    filtered = []
                    for s in service:
                        keep = True
                        if isinstance(s, str) and "cloudfront." in s.lower():
                            keep = False
                        elif isinstance(s, dict) and "Fn::Sub" in s:
                            fn_sub = s["Fn::Sub"]
                            if (
                                isinstance(fn_sub, str)
                                and "cloudfront." in fn_sub.lower()
                            ):
                                keep = False
                        if keep:
                            filtered.append(s)
                    if not filtered:
                        should_remove = True
                    elif len(filtered) != len(service):
                        statement = statement.copy()
                        statement["Principal"] = principal.copy()
                        statement["Principal"]["Service"] = filtered

            if not should_remove:
                cleaned_statements.append(statement)

        policy_doc["Statement"] = cleaned_statements

    def _clean_parameter_groups(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """Clean parameter groups in Metadata to remove references to deleted parameters."""
        metadata = template.get("Metadata", {})
        interface = metadata.get("AWS::CloudFormation::Interface", {})
        parameter_groups = interface.get("ParameterGroups", [])

        if not parameter_groups:
            return template

        cleaned_groups: List[Any] = []
        for group in parameter_groups:
            group_label = group.get("Label", {}).get("default", "")
            if group_label in self.parameter_groups_to_remove:
                logger.debug(f"Removing parameter group: {group_label}")
                continue

            original_params = group.get("Parameters", [])
            cleaned_params = [
                p for p in original_params if p not in self.parameters_to_remove
            ]
            if cleaned_params:
                group["Parameters"] = cleaned_params
                cleaned_groups.append(group)

        interface["ParameterGroups"] = cleaned_groups

        # Clean parameter labels
        parameter_labels = interface.get("ParameterLabels", {})
        for param_name in list(parameter_labels.keys()):
            if param_name in self.parameters_to_remove:
                del parameter_labels[param_name]

        return template

    def _update_configuration_maps_for_govcloud(
        self, template: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update configuration maps and parameters for GovCloud defaults."""
        logger.info("Updating configuration maps for GovCloud deployment")

        mappings = template.get("Mappings", {})
        parameters = template.get("Parameters", {})

        if "ConfigurationMap" in mappings:
            mappings["ConfigurationMap"]["lending-package-sample-govcloud"] = {
                "ConfigPath": "lending-package-sample-govcloud"
            }
            logger.debug("Added lending-package-sample-govcloud to ConfigurationMap")

        if "ConfigurationPreset" in parameters:
            parameters["ConfigurationPreset"]["Default"] = (
                "lending-package-sample-govcloud"
            )
            allowed_values = parameters["ConfigurationPreset"].get("AllowedValues", [])
            if "lending-package-sample-govcloud" not in allowed_values:
                allowed_values.insert(0, "lending-package-sample-govcloud")
                parameters["ConfigurationPreset"]["AllowedValues"] = allowed_values
            logger.info(
                "Updated ConfigurationPreset default to 'lending-package-sample-govcloud'"
            )

        return template

    def _update_arn_partitions(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """Check and fix any hard-coded ARN partitions."""
        logger.info("Checking ARN partitions")
        template_str = yaml.dump(template, default_flow_style=False)

        remaining_arns = len(
            re.findall(
                r"arn:aws:(?!\$\{AWS::Partition\})", template_str
            )  # arn-partition-ok: regex that DETECTS this pattern
        )
        if remaining_arns > 0:
            logger.warning(f"Found {remaining_arns} hard-coded ARN references — fixing")
            template_str = re.sub(
                r"arn:aws:(?!\$\{AWS::Partition\})",  # arn-partition-ok: regex that DETECTS this pattern
                "arn:${AWS::Partition}:",
                template_str,
            )
            template = yaml.safe_load(template_str)
            logger.info(f"Fixed {remaining_arns} hard-coded ARN references")
        else:
            logger.info("✅ All ARN references are already partition-aware")

        return template

    def _update_description(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """Update template description to indicate headless version."""
        current_description = template.get("Description", "")
        if (
            "Headless" not in current_description
            and "GovCloud" not in current_description
        ):
            template["Description"] = current_description + " (Headless)"
            logger.debug("Updated template description for headless deployment")
        return template


class GovCloudTemplateTransformer:
    """Transform an IDP CloudFormation template for GovCloud (CloudFront-free).

    Unlike :class:`HeadlessTemplateTransformer` (which removes the entire UI),
    this transform KEEPS the full Web UI but makes it hostable in GovCloud,
    where the ``AWS::CloudFront::*`` resource types do not exist. cfn-lint /
    CloudFormation reject a template that merely *declares* those types even
    behind a false ``Condition`` (e.g. ``E3006 Resource type
    'AWS::CloudFront::Distribution' does not exist in 'us-gov-west-1'``), so the
    resources must be physically removed and the sole hosting option forced to
    API Gateway (see the ``WebUIHosting=APIGateway`` S3-proxy hosting mode).

    The transform:
      1. Collapses every ``Fn::If`` whose condition is ``UseCloudFrontHosting``
         to its else-branch (the API-Gateway path). This rewrites all CloudFront
         *references* — ``${CloudFrontDistribution.DomainName}``, the OAC/CSP
         refs, the ``CLOUDFRONT_DISTRIBUTION_ID`` env var, ``VITE_UI_BASE_PATH``,
         etc. — to their non-CloudFront values in one pass.
      2. Deletes the three CloudFront resources (``CloudFrontDistribution``,
         ``CloudFrontOriginAccessControl``, ``SecurityHeadersPolicy``) and the
         now-unused ``UseCloudFrontHosting`` condition and CloudFront-only
         parameters (``CloudFrontPriceClass``, ``CloudFrontAllowedGeos``).
      3. Forces ``WebUIHosting`` to ``APIGateway`` (removes ``CloudFront`` from
         AllowedValues and sets it as the Default) so the API-Gateway S3-proxy
         hosting is the only option.
      4. Fixes hard-coded ARN partitions (``arn:aws`` -> ``arn:${AWS::Partition}``)
         and updates the template description.

    Usage::

        transformer = GovCloudTemplateTransformer()
        transformer.transform(input_path, output_path)
    """

    # The AWS::CloudFront::* logical resources to remove.
    CLOUDFRONT_RESOURCES = {
        "CloudFrontDistribution",
        "CloudFrontOriginAccessControl",
        "SecurityHeadersPolicy",
    }
    # CloudFront-only parameters that become dead once CloudFront is removed.
    CLOUDFRONT_PARAMETERS = {
        "CloudFrontPriceClass",
        "CloudFrontAllowedGeos",
    }
    # Conditions that reference CloudFront and are removed after collapsing.
    CLOUDFRONT_CONDITIONS = {
        "UseCloudFrontHosting",
        "ShouldEnableGeoRestriction",
    }
    # The condition whose Fn::If blocks are collapsed to the else-branch.
    HOSTING_CONDITION = "UseCloudFrontHosting"

    # Markers identifying the public AWS Lambda Web Adapter (LWA) layer. LWA is
    # published ONLY in the commercial partition (account 753240598075); AWS
    # account IDs do not exist across partitions, so `arn:${AWS::Partition}:`
    # substitution cannot make that ARN resolvable in GovCloud — the deploy
    # fails with a 403 on lambda:GetLayerVersion that *reads* like a missing
    # resource-based policy but is really a partition mismatch that no identity
    # policy can fix. Any function layering LWA in is therefore uncreatable in
    # GovCloud and must be removed, not merely have its Function URL stripped.
    LWA_LAYER_MARKERS = ("LambdaAdapterLayer", "753240598075")
    # Parameters/conditions that exist only to override the LWA layer ARN and
    # become dead once every LWA-dependent function is gone.
    LWA_PARAMETERS = {"LambdaWebAdapterLayerArn"}
    LWA_CONDITIONS = {"HasLambdaWebAdapterLayerArn"}

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        # Logical ids this transform deleted. Nothing in the output template may
        # still reference them (validated in validate_no_cloudfront).
        self._removed_logical_ids: Set[str] = set()

    # ---- Entry points ----

    def transform(self, input_path: str, output_path: str) -> bool:
        """Transform a template file for GovCloud and write it to output_path.

        Returns True on success.
        """
        try:
            logger.info("🔧 Starting GovCloud (CloudFront-free) transformation")
            template = self.load_template(input_path)
            template = self.apply_transforms(template)
            self.save_template(template, output_path)
            if not self.validate_no_cloudfront(template):
                return False
            logger.info("🎉 GovCloud template transformation completed successfully!")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to generate GovCloud template: {e}")
            if self.verbose:
                import traceback

                logger.debug(traceback.format_exc())
            return False

    def apply_transforms(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """Apply all GovCloud transformations to an in-memory template dict."""
        self._removed_logical_ids = set()
        # 1. Collapse Fn::If[UseCloudFrontHosting] -> else-branch everywhere.
        n_collapsed = [0]
        template = self._collapse_hosting_ifs(template, n_collapsed)
        logger.info(
            f"Collapsed {n_collapsed[0]} Fn::If[{self.HOSTING_CONDITION}] to the "
            "API-Gateway (else) branch"
        )
        # 2. Remove CloudFront resources / condition / params / outputs.
        template = self._remove_cloudfront_resources(template)
        template = self._remove_cloudfront_parameters(template)
        template = self._remove_cloudfront_conditions(template)
        template = self._remove_cloudfront_outputs(template)
        template = self._remove_cloudfront_policy_statements(template)
        template = self._clean_parameter_groups(template)
        # 3. Remove AWS::Lambda::Url resources — Lambda Function URLs are NOT
        #    available in GovCloud (per the AWS GovCloud Lambda docs), so they
        #    also raise E3006. The only one is the chat-streaming endpoint; the
        #    UI degrades gracefully (chat streaming disabled) when its
        #    VITE_STREAM_URL is empty.
        template = self._remove_lambda_function_urls(template)
        # 3b. Remove the functions that CANNOT exist in GovCloud because they
        #     layer in the AWS Lambda Web Adapter (commercial-partition-only
        #     publisher account). Stripping only the Function URL (step 3) left
        #     its handler behind, and the stack rolled back on a 403
        #     lambda:GetLayerVersion for the LWA layer. The Web UI's chat still
        #     works: it auto-detects the empty VITE_STREAM_URL and falls back to
        #     the polling transport, which is served by the (retained)
        #     AgentChatProcessorFunction / ChatWithDocumentProcessorFunction via
        #     the UI REST API — the streaming function is not in that path.
        template = self._remove_lwa_dependent_functions(template)
        # 4. Force WebUIHosting=APIGateway.
        template = self._force_apigateway_hosting(template)
        # 5. Default the Knowledge Base vector store to OpenSearch Serverless —
        #    S3 Vectors is not yet supported for Bedrock Knowledge Bases in
        #    GovCloud (ValidationException: "KnowledgeBase storage type
        #    S3_VECTORS is not supported"), so the commercial default would
        #    fail DOCUMENTKB creation on every GovCloud deploy.
        template = self._force_opensearch_vector_store(template)
        # 6. Default ConfigurationPreset to the GovCloud-safe sample config.
        #    The commercial default (lending-package-sample) pins Bedrock
        #    model IDs (e.g. us.amazon.nova-2-lite-v1:0) that either don't
        #    exist or aren't offered in GovCloud regions, so every processing
        #    invocation would fail with ValidationException: "The provided
        #    model identifier is invalid." until a caller manually selects
        #    lending-package-sample-govcloud.
        template = self._update_configuration_maps_for_govcloud(template)
        # 7. Default KnowledgeBaseModelId to a GovCloud-verified model. The
        #    commercial default (us.amazon.nova-pro-v1:0) uses a us. cross-
        #    region inference-profile prefix that doesn't exist in GovCloud
        #    (ValidationException: "The provided model identifier is
        #    invalid."), which would fail every Knowledge Base query.
        template = self._force_govcloud_kb_model(template)
        # 8. Partition + description.
        template = self._update_arn_partitions(template)
        template = self._update_description(template)
        return template

    # ---- I/O (packaged templates use long-form Fn:: intrinsics) ----

    def load_template(self, input_file: str) -> Dict[str, Any]:
        logger.info(f"Loading template from {input_file}")
        if not os.path.exists(input_file):
            raise FileNotFoundError(f"Input template file not found: {input_file}")
        with open(input_file, "r", encoding="utf-8") as f:
            template = yaml.safe_load(f)
        logger.debug(
            f"Loaded template with {len(template.get('Resources', {}))} resources"
        )
        return template

    def save_template(self, template: Dict[str, Any], output_file: str) -> None:
        logger.info(f"Saving GovCloud template to {output_file}")
        os.makedirs(
            os.path.dirname(output_file) if os.path.dirname(output_file) else ".",
            exist_ok=True,
        )
        with open(output_file, "w", encoding="utf-8") as f:
            yaml.dump(template, f, default_flow_style=False, width=120, indent=2)
        logger.info("✅ GovCloud template saved successfully")

    # ---- Fn::If collapsing ----

    def _collapse_hosting_ifs(self, node: Any, counter: List[int]) -> Any:
        """Recursively collapse Fn::If[UseCloudFrontHosting] to its else-branch.

        A CloudFormation ``Fn::If`` is ``{"Fn::If": [condition, then, else]}``.
        For the hosting condition we always take the else-branch (index 2), which
        is the API-Gateway path in this template. All other Fn::If nodes are left
        intact (their subtrees are still recursed into).
        """
        if isinstance(node, dict):
            if (
                "Fn::If" in node
                and isinstance(node["Fn::If"], list)
                and len(node["Fn::If"]) == 3
                and node["Fn::If"][0] == self.HOSTING_CONDITION
            ):
                counter[0] += 1
                # Recurse into the chosen (else) branch in case of nesting.
                return self._collapse_hosting_ifs(node["Fn::If"][2], counter)
            return {k: self._collapse_hosting_ifs(v, counter) for k, v in node.items()}
        if isinstance(node, list):
            return [self._collapse_hosting_ifs(v, counter) for v in node]
        return node

    # ---- Removal helpers ----

    def _remove_cloudfront_resources(self, template: Dict[str, Any]) -> Dict[str, Any]:
        resources = template.get("Resources", {})
        for name in self.CLOUDFRONT_RESOURCES:
            if name in resources:
                del resources[name]
                logger.debug(f"Removed CloudFront resource: {name}")
        # Defensive: drop any remaining AWS::CloudFront::* resource by type.
        for name in list(resources.keys()):
            rtype = (
                resources[name].get("Type", "")
                if isinstance(resources[name], dict)
                else ""
            )
            if isinstance(rtype, str) and rtype.startswith("AWS::CloudFront::"):
                del resources[name]
                logger.debug(f"Removed CloudFront-typed resource: {name} ({rtype})")
        return template

    def _remove_cloudfront_parameters(self, template: Dict[str, Any]) -> Dict[str, Any]:
        params = template.get("Parameters", {})
        for name in self.CLOUDFRONT_PARAMETERS:
            if name in params:
                del params[name]
                logger.debug(f"Removed CloudFront parameter: {name}")
        return template

    def _remove_cloudfront_conditions(self, template: Dict[str, Any]) -> Dict[str, Any]:
        conditions = template.get("Conditions", {})
        for name in self.CLOUDFRONT_CONDITIONS:
            if name in conditions:
                del conditions[name]
                logger.debug(f"Removed CloudFront condition: {name}")
        return template

    def _remove_cloudfront_outputs(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """Remove Outputs whose Condition was a removed CloudFront condition."""
        outputs = template.get("Outputs", {})
        for name in list(outputs.keys()):
            out = outputs[name]
            if (
                isinstance(out, dict)
                and out.get("Condition") in self.CLOUDFRONT_CONDITIONS
            ):
                del outputs[name]
                logger.debug(f"Removed CloudFront-conditioned output: {name}")
        return template

    def _remove_lambda_function_urls(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """Remove AWS::Lambda::Url resources (unavailable in GovCloud).

        Also removes the associated AWS::Lambda::Permission statements that grant
        lambda:InvokeFunctionUrl for those URLs, drops any Outputs that GetAtt the
        removed URL's FunctionUrl, and blanks CodeBuild env / test-env references
        (e.g. VITE_STREAM_URL) so the UI build still succeeds. The Web UI degrades
        gracefully — the only Function URL here is the chat-streaming endpoint,
        which is simply disabled when its stream URL is empty.
        """
        resources = template.get("Resources", {})
        url_resources = {
            name
            for name, res in resources.items()
            if isinstance(res, dict) and res.get("Type") == "AWS::Lambda::Url"
        }
        if not url_resources:
            return template

        # Remove the URL resources themselves.
        for name in url_resources:
            del resources[name]
            logger.debug(f"Removed AWS::Lambda::Url resource: {name}")

        # Remove Lambda permissions that grant InvokeFunctionUrl (they only exist
        # to authorize the now-removed Function URLs).
        for name in list(resources.keys()):
            res = resources[name]
            if (
                not isinstance(res, dict)
                or res.get("Type") != "AWS::Lambda::Permission"
            ):
                continue
            action = res.get("Properties", {}).get("Action", "")
            if action == "lambda:InvokeFunctionUrl":
                del resources[name]
                logger.debug(f"Removed InvokeFunctionUrl permission: {name}")

        # Remove Outputs that reference a removed URL's .FunctionUrl.
        outputs = template.get("Outputs", {})
        for name in list(outputs.keys()):
            if self._references_function_url(outputs[name], url_resources):
                del outputs[name]
                logger.debug(
                    f"Removed output referencing a removed Function URL: {name}"
                )

        # Blank remaining .FunctionUrl references (e.g. VITE_STREAM_URL in the UI
        # CodeBuild env, or Fn::Sub'd values) so nothing dangles. The UI treats an
        # empty VITE_STREAM_URL as "chat streaming disabled".
        removed = self._blank_function_url_refs(resources, url_resources)
        removed += self._blank_function_url_refs(outputs, url_resources)
        if removed:
            logger.info(
                f"Blanked {removed} reference(s) to removed Lambda Function URL(s)"
            )
        logger.info(
            f"Removed {len(url_resources)} AWS::Lambda::Url resource(s) "
            "(not available in GovCloud)"
        )
        return template

    @staticmethod
    def _references_function_url(node: Any, url_names: Set[str]) -> bool:
        """True if node contains a GetAtt/Sub referencing <url>.FunctionUrl."""
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "Fn::GetAtt":
                    parts = v if isinstance(v, list) else str(v).split(".")
                    if (
                        len(parts) >= 2
                        and parts[0] in url_names
                        and parts[1] == "FunctionUrl"
                    ):
                        return True
                if k == "Fn::Sub":
                    s = v[0] if isinstance(v, list) else v
                    if isinstance(s, str):
                        for u in url_names:
                            if f"{u}.FunctionUrl" in s:
                                return True
                if GovCloudTemplateTransformer._references_function_url(v, url_names):
                    return True
        elif isinstance(node, list):
            return any(
                GovCloudTemplateTransformer._references_function_url(x, url_names)
                for x in node
            )
        return False

    def _blank_function_url_refs(self, node: Any, url_names: Set[str]) -> int:
        """Replace {Fn::GetAtt: [url, FunctionUrl]} / Fn::Sub with "" in-place.

        Returns the number of references blanked.
        """
        count = 0
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if isinstance(v, dict) and "Fn::GetAtt" in v:
                    ga = v["Fn::GetAtt"]
                    parts = ga if isinstance(ga, list) else str(ga).split(".")
                    if (
                        len(parts) >= 2
                        and parts[0] in url_names
                        and parts[1] == "FunctionUrl"
                    ):
                        node[k] = ""
                        count += 1
                        continue
                if isinstance(v, dict) and "Fn::Sub" in v:
                    s = v["Fn::Sub"]
                    sub_str = s[0] if isinstance(s, list) else s
                    if isinstance(sub_str, str) and any(
                        f"{u}.FunctionUrl" in sub_str for u in url_names
                    ):
                        # Replace the ${url.FunctionUrl} token with empty string.
                        for u in url_names:
                            sub_str = sub_str.replace(f"${{{u}.FunctionUrl}}", "")
                        node[k] = sub_str
                        count += 1
                        continue
                count += self._blank_function_url_refs(v, url_names)
        elif isinstance(node, list):
            for x in node:
                count += self._blank_function_url_refs(x, url_names)
        return count

    # ---- LWA-dependent function removal (see LWA_LAYER_MARKERS) ----

    def _remove_lwa_dependent_functions(
        self, template: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Remove Lambda functions that layer in the AWS Lambda Web Adapter.

        The LWA layer is published only in the commercial partition, so any
        function referencing it cannot be created in GovCloud (403 on
        lambda:GetLayerVersion). Removing the function is not enough on its own:
        its log group, its Lambda permissions, the IAM statements that grant
        invoke on it, and any output referencing it must go too — and an inline
        IAM policy left with ``Statement: []`` is itself rejected by IAM
        ("Syntax errors in policy", 400), so emptied policies are dropped
        entirely rather than left behind.
        """
        resources = template.get("Resources", {})
        functions = {
            name for name, res in resources.items() if self._uses_lwa_layer(res)
        }
        if not functions:
            return template

        log_groups = self._referenced_log_groups(resources, functions)

        for name in functions:
            del resources[name]
            logger.debug(f"Removed LWA-dependent function: {name}")

        # Drop each function's log group, but only if nothing else uses it.
        orphaned_log_groups = set()
        for lg in log_groups:
            if lg not in resources:
                continue
            definition = resources.pop(lg)
            if self._logical_id_referenced(template, lg):
                resources[lg] = definition  # still used elsewhere — keep it
            else:
                orphaned_log_groups.add(lg)
                logger.debug(f"Removed orphaned log group: {lg}")

        self._removed_logical_ids |= functions | orphaned_log_groups
        self._prune_references_to(template, self._removed_logical_ids)
        self._remove_lwa_parameters_and_conditions(template)

        logger.info(
            f"Removed {len(functions)} AWS Lambda Web Adapter-dependent "
            f"function(s) (LWA layer is commercial-partition only): "
            f"{', '.join(sorted(functions))}"
        )
        return template

    @classmethod
    def _uses_lwa_layer(cls, resource: Any) -> bool:
        """True if a Lambda/Serverless function's Layers reference the LWA layer."""
        if not isinstance(resource, dict):
            return False
        rtype = resource.get("Type")
        if rtype not in ("AWS::Serverless::Function", "AWS::Lambda::Function"):
            return False
        layers = resource.get("Properties", {}).get("Layers")
        if layers is None:
            return False
        # The layer ARN sits behind an Fn::If (parameter override vs. default),
        # so match on the serialized subtree rather than a fixed shape.
        blob = yaml.dump(layers, default_flow_style=False)
        return any(marker in blob for marker in cls.LWA_LAYER_MARKERS)

    @staticmethod
    def _referenced_log_groups(
        resources: Dict[str, Any], function_names: Set[str]
    ) -> Set[str]:
        """Logical ids of log groups named by the given functions' LoggingConfig."""
        found: Set[str] = set()
        for name in function_names:
            logging_config = (
                resources.get(name, {}).get("Properties", {}).get("LoggingConfig", {})
            )
            log_group = (
                logging_config.get("LogGroup")
                if isinstance(logging_config, dict)
                else None
            )
            if isinstance(log_group, dict) and isinstance(log_group.get("Ref"), str):
                found.add(log_group["Ref"])
        return found

    @staticmethod
    def _logical_id_referenced(template: Dict[str, Any], logical_id: str) -> bool:
        """True if logical_id appears anywhere in the (already-pruned) template.

        The caller pops the resource's own definition first, so a hit here means
        a genuine Ref/GetAtt/Sub/DependsOn from some other place.
        """
        return logical_id in yaml.dump(template, default_flow_style=False)

    def _prune_references_to(self, template: Dict[str, Any], removed: Set[str]) -> None:
        """Strip every remaining reference to the removed logical ids.

        Handles the four shapes that actually occur: Lambda permissions targeting
        a removed function, IAM policy statements whose Resource points at one,
        DependsOn entries, and Outputs.
        """
        if not removed:
            return
        # Snapshot: pruning can itself remove resources (a Lambda permission
        # targeting a removed function), and those names must be pruned from
        # DependsOn/Outputs too — but `removed` may be the instance's own set, so
        # grow a local copy rather than the set being iterated against.
        targets = set(removed)
        resources = template.get("Resources", {})

        # 1. Lambda permissions whose FunctionName is a removed function.
        for name in list(resources.keys()):
            res = resources[name]
            if (
                isinstance(res, dict)
                and res.get("Type") == "AWS::Lambda::Permission"
                and self._node_references(
                    res.get("Properties", {}).get("FunctionName"), targets
                )
            ):
                del resources[name]
                self._removed_logical_ids.add(name)
                targets.add(name)
                logger.debug(f"Removed Lambda permission for removed function: {name}")

        # 2. IAM statements / policies. An inline policy emptied of statements is
        #    dropped: IAM rejects `Statement: []` with a 400 syntax error.
        for name in list(resources.keys()):
            res = resources[name]
            if not isinstance(res, dict):
                continue
            props = res.get("Properties", {})
            if not isinstance(props, dict):
                continue

            # Role-style inline policies: Properties.Policies[*].PolicyDocument
            policies = props.get("Policies")
            if isinstance(policies, list):
                kept_policies = []
                dropped_any = False
                for policy in policies:
                    if not isinstance(policy, dict):
                        kept_policies.append(policy)
                        continue
                    doc = policy.get("PolicyDocument")
                    if not isinstance(doc, dict):
                        kept_policies.append(policy)
                        continue
                    if self._prune_statements(doc, targets):
                        kept_policies.append(policy)
                    else:
                        dropped_any = True
                        logger.debug(
                            f"Dropped now-empty inline policy "
                            f"{policy.get('PolicyName', '<unnamed>')} on {name} "
                            "(IAM rejects Statement: [])"
                        )
                # Only rewrite Policies when this pass actually dropped one.
                # Touching it unconditionally rewrote unrelated resources (e.g.
                # stripping an intentional `Policies: []`), which widened the
                # GovCloud template diff and made the transform harder to audit.
                if dropped_any:
                    if kept_policies:
                        props["Policies"] = kept_policies
                    else:
                        props.pop("Policies", None)
                        logger.debug(f"Dropped now-empty Policies list on {name}")

            # Standalone policy documents (ManagedPolicy, IAM::Policy, bucket
            # policies): Properties.PolicyDocument. Only remove the resource if
            # pruning is what emptied it — a policy that was already statement-
            # less is not ours to delete.
            doc = props.get("PolicyDocument")
            if isinstance(doc, dict) and doc.get("Statement"):
                if not self._prune_statements(doc, targets):
                    del resources[name]
                    self._removed_logical_ids.add(name)
                    targets.add(name)
                    logger.debug(f"Removed policy resource emptied by pruning: {name}")

        # 3. DependsOn entries pointing at removed resources.
        for res in resources.values():
            if not isinstance(res, dict) or "DependsOn" not in res:
                continue
            deps = res["DependsOn"]
            if isinstance(deps, str):
                if deps in targets:
                    del res["DependsOn"]
            elif isinstance(deps, list):
                kept = [d for d in deps if d not in targets]
                if kept:
                    res["DependsOn"] = kept
                else:
                    del res["DependsOn"]

        # 4. Outputs referencing a removed resource.
        outputs = template.get("Outputs", {})
        for name in list(outputs.keys()):
            if self._node_references(outputs[name], targets):
                del outputs[name]
                logger.debug(f"Removed output referencing a removed resource: {name}")

    def _prune_statements(self, doc: Dict[str, Any], removed: Set[str]) -> bool:
        """Prune statements referencing removed ids. Returns True if any remain.

        A statement whose ``Resource`` list only partly references removed ids
        keeps its surviving entries; one left with no resources at all is
        dropped, since an Allow with an empty Resource is invalid.
        """
        statements = doc.get("Statement")
        if isinstance(statements, dict):
            statements = [statements]
        if not isinstance(statements, list):
            return True

        kept: List[Any] = []
        for stmt in statements:
            if not isinstance(stmt, dict):
                kept.append(stmt)
                continue
            resource = stmt.get("Resource")
            if isinstance(resource, list):
                surviving = [
                    r for r in resource if not self._node_references(r, removed)
                ]
                if not surviving:
                    continue
                if len(surviving) != len(resource):
                    stmt = {**stmt, "Resource": surviving}
            elif self._node_references(resource, removed):
                continue
            kept.append(stmt)

        doc["Statement"] = kept
        return bool(kept)

    @classmethod
    def _node_references(cls, node: Any, names: Set[str]) -> bool:
        """True if node contains a Ref/GetAtt/Sub pointing at one of names."""
        if not names:
            return False
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "Ref" and isinstance(value, str) and value in names:
                    return True
                if key == "Fn::GetAtt":
                    parts = value if isinstance(value, list) else str(value).split(".")
                    if parts and parts[0] in names:
                        return True
                if key == "Fn::Sub":
                    text = value[0] if isinstance(value, list) else value
                    # Match the WHOLE logical id, not a prefix: "${FooAlarm}" is
                    # NOT a reference to a removed "Foo". A ${...} token either
                    # closes with "}" or continues with ".Attribute", so require
                    # one of those to follow the name — otherwise a surviving
                    # resource whose name merely starts with a removed one would
                    # have its IAM statement silently pruned.
                    if isinstance(text, str) and any(
                        f"${{{n}}}" in text or f"${{{n}." in text for n in names
                    ):
                        return True
                if cls._node_references(value, names):
                    return True
        elif isinstance(node, list):
            return any(cls._node_references(x, names) for x in node)
        return False

    def _remove_lwa_parameters_and_conditions(self, template: Dict[str, Any]) -> None:
        """Drop the LWA layer-override parameter and its condition once dead.

        Leaving ``LambdaWebAdapterLayerArn`` in a GovCloud template would keep
        advertising an override with no target — the base template's comment
        suggested GovCloud publishes LWA under a different account, but no
        GovCloud LWA publication exists (the LWA project documents commercial
        layer ARNs only).
        """
        params = template.get("Parameters", {})
        conditions = template.get("Conditions", {})
        # Conditions FIRST: `HasLambdaWebAdapterLayerArn` contains the parameter
        # name as a substring, so a surviving condition would make the
        # still-in-use probe below report a false positive for the parameter.
        for name in self.LWA_CONDITIONS:
            if name in conditions and not self._condition_still_used(template, name):
                del conditions[name]
                logger.debug(f"Removed dead LWA condition: {name}")
        for name in self.LWA_PARAMETERS:
            if name in params and not self._parameter_still_used(template, name):
                del params[name]
                logger.debug(f"Removed dead LWA parameter: {name}")
        # Tidy the console UI so no group/label points at a removed parameter.
        interface = template.get("Metadata", {}).get(
            "AWS::CloudFormation::Interface", {}
        )
        for group in interface.get("ParameterGroups", []) or []:
            plist = group.get("Parameters", [])
            group["Parameters"] = [p for p in plist if p in params]
        labels = interface.get("ParameterLabels", {}) or {}
        for p in list(labels.keys()):
            if p in self.LWA_PARAMETERS and p not in params:
                del labels[p]

    @staticmethod
    def _parameter_still_used(template: Dict[str, Any], name: str) -> bool:
        """True if anything outside Parameters/Metadata still uses name.

        Parameters holds the declaration itself; Metadata only holds console
        groups/labels, which are tidied separately. Rules ARE probed — a Rule
        referencing a deleted parameter is a template error.
        """
        probe = {
            k: v for k, v in template.items() if k not in ("Parameters", "Metadata")
        }
        return name in yaml.dump(probe, default_flow_style=False)

    @staticmethod
    def _condition_still_used(template: Dict[str, Any], name: str) -> bool:
        probe = {k: v for k, v in template.items() if k != "Conditions"}
        return name in yaml.dump(probe, default_flow_style=False)

    def _remove_cloudfront_policy_statements(
        self, template: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Drop IAM/bucket policy statements scoped to the CloudFront service.

        The LoggingBucket grants ``cloudfront.<urlsuffix>`` log-delivery write
        access (Sid ``AllowCloudFrontLogs``). With CloudFront removed this is
        dead, and the CloudFront service principal does not exist in GovCloud —
        some IAM validators reject an Allow to a nonexistent service principal —
        so strip any statement whose Principal.Service resolves to the
        CloudFront service.
        """
        resources = template.get("Resources", {})
        removed = 0
        for res in resources.values():
            if not isinstance(res, dict):
                continue
            props = res.get("Properties", {})
            policy = props.get("PolicyDocument", {})
            statements = policy.get("Statement") if isinstance(policy, dict) else None
            if not isinstance(statements, list):
                continue
            kept = []
            for stmt in statements:
                if isinstance(stmt, dict) and self._is_cloudfront_service_statement(
                    stmt
                ):
                    removed += 1
                    continue
                kept.append(stmt)
            policy["Statement"] = kept
        if removed:
            logger.info(f"Removed {removed} CloudFront-service policy statement(s)")
        return template

    @staticmethod
    def _is_cloudfront_service_statement(stmt: Dict[str, Any]) -> bool:
        principal = stmt.get("Principal")
        if not isinstance(principal, dict):
            return False
        service = principal.get("Service")
        candidates = service if isinstance(service, list) else [service]
        for svc in candidates:
            # Service may be a plain string or an Fn::Sub dict.
            text = ""
            if isinstance(svc, str):
                text = svc
            elif isinstance(svc, dict):
                text = str(svc.get("Fn::Sub", svc))
            if "cloudfront." in text:
                return True
        return False

    def _force_apigateway_hosting(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """Force WebUIHosting to APIGateway (the only GovCloud-viable option)."""
        params = template.get("Parameters", {})
        webui = params.get("WebUIHosting")
        if isinstance(webui, dict):
            webui["AllowedValues"] = ["APIGateway"]
            webui["Default"] = "APIGateway"
            webui["Description"] = (
                "Frontend hosting method. Fixed to APIGateway for GovCloud "
                "(CloudFront is not available in GovCloud regions). The Web UI is "
                "served as an S3 proxy on the REST API; set "
                "ApiGatewayVisibility=PRIVATE for VPC-only access."
            )
            logger.info("Forced WebUIHosting=APIGateway (CloudFront removed)")
        return template

    def _force_opensearch_vector_store(
        self, template: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Default KnowledgeBaseVectorStore to OpenSearch Serverless.

        S3 Vectors is not yet supported for Bedrock Knowledge Bases in GovCloud
        regions. The commercial template defaults to S3_VECTORS; override the
        default here so a GovCloud deploy that doesn't explicitly pass this
        parameter still succeeds. Both AllowedValues are kept (not forced,
        unlike WebUIHosting) since S3_VECTORS may become available later and
        OpenSearch Serverless requires its own IAM/network setup.
        """
        params = template.get("Parameters", {})
        vector_store = params.get("KnowledgeBaseVectorStore")
        if (
            isinstance(vector_store, dict)
            and vector_store.get("Default") != "OPENSEARCH_SERVERLESS"
        ):
            vector_store["Default"] = "OPENSEARCH_SERVERLESS"
            logger.info(
                "Defaulted KnowledgeBaseVectorStore=OPENSEARCH_SERVERLESS "
                "(S3 Vectors unsupported for Bedrock Knowledge Bases in GovCloud)"
            )
        return template

    # GovCloud-verified value for KnowledgeBaseModelId (see the AllowedValues
    # comment in template.yaml for how these were verified).
    GOVCLOUD_KB_MODEL_DEFAULT = "amazon.nova-pro-v1:0"

    def _force_govcloud_kb_model(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """Default KnowledgeBaseModelId to a GovCloud-verified model.

        The commercial default (us.amazon.nova-pro-v1:0) uses a us.
        cross-region inference-profile prefix that returns ValidationException
        ("The provided model identifier is invalid.") in GovCloud regions —
        none of the us./eu./global. prefixed AllowedValues work there. Only
        override the Default; AllowedValues already carries the GovCloud-safe
        entries (added alongside the commercial ones in template.yaml) so a
        caller can still pick a different verified model explicitly.
        """
        params = template.get("Parameters", {})
        kb_model = params.get("KnowledgeBaseModelId")
        if (
            isinstance(kb_model, dict)
            and kb_model.get("Default") != self.GOVCLOUD_KB_MODEL_DEFAULT
        ):
            kb_model["Default"] = self.GOVCLOUD_KB_MODEL_DEFAULT
            logger.info(
                f"Defaulted KnowledgeBaseModelId={self.GOVCLOUD_KB_MODEL_DEFAULT} "
                "(commercial us./eu./global. inference-profile prefixes are "
                "invalid in GovCloud)"
            )
        return template

    # ---- Parameter-group tidy (drop dangling CloudFront param labels) ----

    def _clean_parameter_groups(self, template: Dict[str, Any]) -> Dict[str, Any]:
        metadata = template.get("Metadata", {})
        interface = metadata.get("AWS::CloudFormation::Interface", {})
        groups = interface.get("ParameterGroups", [])
        removed = self.CLOUDFRONT_PARAMETERS
        for group in groups:
            plist = group.get("Parameters", [])
            group["Parameters"] = [p for p in plist if p not in removed]
        labels = interface.get("ParameterLabels", {})
        for p in list(labels.keys()):
            if p in removed:
                del labels[p]
        return template

    # ---- Configuration preset default (mirrors HeadlessTemplateTransformer's
    # ---- helper of the same name — kept independent since the two classes
    # ---- transform disjoint templates and neither imports the other) ----

    def _update_configuration_maps_for_govcloud(
        self, template: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Default ConfigurationPreset to the GovCloud-safe sample config.

        The commercial default (lending-package-sample) pins Bedrock model
        IDs that either don't exist or aren't offered in GovCloud regions
        (e.g. ``us.amazon.nova-2-lite-v1:0``), so document processing fails
        with ``ValidationException: The provided model identifier is
        invalid.`` until ``lending-package-sample-govcloud`` — which pins
        GovCloud-available models — is selected instead.
        """
        logger.info("Updating configuration maps for GovCloud deployment")

        mappings = template.get("Mappings", {})
        parameters = template.get("Parameters", {})

        if "ConfigurationMap" in mappings:
            mappings["ConfigurationMap"]["lending-package-sample-govcloud"] = {
                "ConfigPath": "lending-package-sample-govcloud"
            }
            logger.debug("Added lending-package-sample-govcloud to ConfigurationMap")

        if "ConfigurationPreset" in parameters:
            parameters["ConfigurationPreset"]["Default"] = (
                "lending-package-sample-govcloud"
            )
            allowed_values = parameters["ConfigurationPreset"].get("AllowedValues", [])
            if "lending-package-sample-govcloud" not in allowed_values:
                allowed_values.insert(0, "lending-package-sample-govcloud")
                parameters["ConfigurationPreset"]["AllowedValues"] = allowed_values
            logger.info(
                "Updated ConfigurationPreset default to 'lending-package-sample-govcloud'"
            )

        return template

    # ---- ARN partition + description (mirror the headless helpers) ----

    def _update_arn_partitions(self, template: Dict[str, Any]) -> Dict[str, Any]:
        template_str = yaml.dump(template, default_flow_style=False)
        remaining = len(
            re.findall(r"arn:aws:(?!\$\{AWS::Partition\})", template_str)
        )  # arn-partition-ok: regex that DETECTS this pattern
        if remaining > 0:
            logger.warning(f"Found {remaining} hard-coded ARN references — fixing")
            template_str = re.sub(
                r"arn:aws:(?!\$\{AWS::Partition\})",  # arn-partition-ok: regex that DETECTS this pattern
                "arn:${AWS::Partition}:",
                template_str,
            )
            template = yaml.safe_load(template_str)
        return template

    def _update_description(self, template: Dict[str, Any]) -> Dict[str, Any]:
        current = template.get("Description", "")
        if "GovCloud" not in current:
            template["Description"] = current + " (GovCloud - CloudFront removed)"
        return template

    # ---- Validation ----

    # Resource types that do not exist in GovCloud regions and would raise
    # cfn-lint E3006 (and fail deployment) if left in the template.
    GOVCLOUD_UNSUPPORTED_TYPE_PREFIXES = ("AWS::CloudFront::",)
    GOVCLOUD_UNSUPPORTED_TYPES = frozenset({"AWS::Lambda::Url"})

    def validate_no_cloudfront(self, template: Dict[str, Any]) -> bool:
        """Assert no GovCloud-unsupported resource types or dangling refs remain.

        Checks for AWS::CloudFront::* and AWS::Lambda::Url (both raise E3006 in
        GovCloud), plus dangling references to the removed CloudFront resources /
        condition. (Name kept for backwards compatibility.)
        """
        issues: List[str] = []
        resources = template.get("Resources", {})
        for name, res in resources.items():
            rtype = res.get("Type", "") if isinstance(res, dict) else ""
            if isinstance(rtype, str) and (
                rtype.startswith(self.GOVCLOUD_UNSUPPORTED_TYPE_PREFIXES)
                or rtype in self.GOVCLOUD_UNSUPPORTED_TYPES
            ):
                issues.append(
                    f"GovCloud-unsupported resource type still present: {name} ({rtype})"
                )

        # No reference to a removed CloudFront logical id should survive.
        template_str = yaml.dump(template, default_flow_style=False)
        for ref in self.CLOUDFRONT_RESOURCES:
            if ref in template_str:
                issues.append(
                    f"Dangling reference to removed CloudFront resource: {ref}"
                )
        if self.HOSTING_CONDITION in template_str:
            issues.append(
                f"Dangling reference to removed condition: {self.HOSTING_CONDITION}"
            )

        # Nothing may still reference a resource this transform deleted (e.g. the
        # LWA-dependent chat streaming function, its log group, or the IAM
        # statement that granted invoke on it). A survivor here is a guaranteed
        # deploy-time rollback, so fail the publish instead.
        #
        # Matched on a word boundary, not a raw substring: a SURVIVING resource
        # whose logical id merely contains a removed one (e.g. a retained
        # "FooAlarm" after "Foo" was removed) must not fail the publish for a
        # non-reason. Surviving logical ids are excluded from the haystack for the
        # same reason.
        surviving = set(resources) | set(template.get("Outputs", {}) or {})
        for ref in sorted(self._removed_logical_ids):
            if any(s != ref and ref in s for s in surviving):
                # A retained id contains this name, so a raw scan would false-
                # positive. Fall back to structural reference detection.
                if self._node_references(template, {ref}):
                    issues.append(f"Dangling reference to removed resource: {ref}")
                continue
            if re.search(rf"\b{re.escape(ref)}\b", template_str):
                issues.append(f"Dangling reference to removed resource: {ref}")

        # An inline IAM policy emptied by pruning must be dropped, not left with
        # an empty statement list — IAM rejects it with "Syntax errors in policy"
        # (400) and the stack rolls back at a different, confusing place.
        for name, res in resources.items():
            if not isinstance(res, dict):
                continue
            props = res.get("Properties", {})
            if not isinstance(props, dict):
                continue
            docs = [
                p.get("PolicyDocument")
                for p in (props.get("Policies") or [])
                if isinstance(p, dict)
            ]
            docs.append(props.get("PolicyDocument"))
            for doc in docs:
                if isinstance(doc, dict) and doc.get("Statement") == []:
                    issues.append(f"Empty IAM policy Statement list on {name}")

        if issues:
            for issue in issues:
                logger.error(f"GovCloud validation issue: {issue}")
            return False
        logger.info(
            "✅ GovCloud template contains no CloudFront / Lambda-URL resources or "
            "dangling refs"
        )
        return True
