"""
Create Bedrock Document Analysis (BDA) blueprints based on extracted labels.
"""

import json
import logging

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class BDABlueprintCreator:
    def __init__(self, region=None):
        """Initialize Bedrock client.

        Args:
            region: Optional AWS region. ``None`` defers to boto3's own
                resolution, which is correct in Lambda. An out-of-region caller
                (``idp-cli config-sync-bda --region …``) must pass it, or the
                blueprints are created against the wrong region's BDA project.
        """
        self.region = region
        self.bedrock_client = boto3.client(
            service_name="bedrock-data-automation", region_name=region
        )

    def update_data_automation_project(self, projectArn: str, blueprint):
        """
        Update an existing Bedrock Data Automation project with the provided blueprint.

        Args:
            projectArn (str): ARN of the project to update
            blueprint (dict): Blueprint configuration to apply

        Returns:
            dict: Updated project details or None if error
        """
        try:
            print(f"blueprint to update {blueprint}")
            project = self.bedrock_client.get_data_automation_project(
                projectArn=projectArn, projectStage="LIVE"
            )
            project = project.get("project", None)
            logger.info(f"Updating project: {project}")
            customOutputConfiguration = project.get("customOutputConfiguration", None)
            if customOutputConfiguration is None:
                customOutputConfiguration = {"blueprints": []}
                project["customOutputConfiguration"] = customOutputConfiguration

            blueprints = customOutputConfiguration.get("blueprints")
            if blueprints is None:
                blueprints = []
                customOutputConfiguration["blueprints"] = blueprints
            _blueprint = {
                "blueprintArn": blueprint.get("blueprintArn"),
            }
            if blueprint.get("blueprintStage"):
                _blueprint["blueprintStage"] = blueprint.get("blueprintStage")
            if blueprint.get("blueprintVersion"):
                _blueprint["blueprintVersion"] = blueprint.get("blueprintVersion")

            for _blueprint_tmp in blueprints:
                if _blueprint_tmp.get("blueprintArn") == blueprint.get("blueprintArn"):
                    blueprints.remove(_blueprint_tmp)
                    break
            blueprints.append(_blueprint)
            logger.info(f"Updating updated data automation project: {projectArn}")
            self.bedrock_client.update_data_automation_project(
                projectArn=projectArn,
                projectDescription=project.get("projectDescription"),
                projectStage=project.get("projectStage"),
                customOutputConfiguration=customOutputConfiguration,
                standardOutputConfiguration=project.get(
                    "standardOutputConfiguration", None
                ),
                overrideConfiguration=project.get("overrideConfiguration", None),
            )
            logger.info(f"Successfully updated data automation project: {projectArn}")
            return _blueprint
        except ClientError as e:
            logger.error(f"Failed to update data automation project: {e}")
            return None

    def update_project_with_custom_configurations(
        self, projectArn: str, customConfiguration
    ):
        """
        Update an existing Bedrock Data Automation project with the provided blueprint.

        Args:
            projectArn (str): ARN of the project to update
            blueprint (dict): Blueprint configuration to apply

        Returns:
            dict: Updated project details or None if error
        """
        try:
            project = self.bedrock_client.get_data_automation_project(
                projectArn=projectArn, projectStage="LIVE"
            )
            project = project.get("project", None)
            logger.info(f"Updating project: {project}")

            logger.info(f"Updating updated data automation project: {projectArn}")
            response = self.bedrock_client.update_data_automation_project(
                projectArn=projectArn,
                projectDescription=project.get("projectDescription"),
                projectStage=project.get("projectStage"),
                customOutputConfiguration=customConfiguration,
                standardOutputConfiguration=project.get(
                    "standardOutputConfiguration", None
                ),
                overrideConfiguration=project.get("overrideConfiguration", None),
            )
            logger.info(f"Successfully updated data automation project: {projectArn}")
            return response
        except ClientError as e:
            logger.error(f"Failed to update data automation project: {e}")
            return None

    def create_data_automation_project(self, project_name, description, blueprint_arn):
        """
        Create a Bedrock Data Automation project.

        Args:
            project_name (str): Name of the project
            description (str, optional): Project description

        Returns:
            dict: Created project details or None if error
            TODO: Fix the signature accept blueprint object instead of schema
        """
        try:
            params = {"name": project_name}

            if description:
                params["description"] = description

            response = self.bedrock_client.create_data_automation_project(
                projectName=project_name,
                projectDescription=description,
                projectStage="LIVE",
                standardOutputConfiguration={
                    "document": {
                        "extraction": {
                            "granularity": {
                                "types": [
                                    "PAGE",
                                    "ELEMENT",
                                ]
                            },
                            "boundingBox": {"state": "DISABLED"},
                        },
                        "generativeField": {"state": "DISABLED"},
                        "outputFormat": {
                            "textFormat": {"types": ["MARKDOWN"]},
                            "additionalFileFormat": {"state": "DISABLED"},
                        },
                    },
                    "image": {
                        "extraction": {
                            "category": {
                                "state": "ENABLED",
                                "types": ["TEXT_DETECTION"],
                            },
                            "boundingBox": {"state": "ENABLED"},
                        },
                        "generativeField": {
                            "state": "ENABLED",
                            "types": ["IMAGE_SUMMARY"],
                        },
                    },
                    "video": {
                        "extraction": {
                            "category": {
                                "state": "ENABLED",
                                "types": [
                                    "TEXT_DETECTION",
                                ],
                            },
                            "boundingBox": {"state": "ENABLED"},
                        },
                        "generativeField": {
                            "state": "ENABLED",
                            "types": [
                                "VIDEO_SUMMARY",
                                "CHAPTER_SUMMARY",
                            ],
                        },
                    },
                    "audio": {
                        "extraction": {
                            "category": {"state": "ENABLED", "types": ["TRANSCRIPT"]}
                        },
                        "generativeField": {"state": "DISABLED"},
                    },
                },
                customOutputConfiguration={
                    "blueprints": [
                        {"blueprintArn": blueprint_arn, "blueprintStage": "LIVE"},
                    ]
                },
                overrideConfiguration={"document": {"splitter": {"state": "ENABLED"}}},
            )

            return response
        except ClientError as e:
            logger.error(f"Error creating Data Automation project: {e}")
            return None

    def create_blueprint(self, document_type, blueprint_name, schema=None):
        """
        Create a Bedrock Document Analysis blueprint.

        Args:
            document_type (str): Type of document
            blueprint_name (str): Name for the blueprint
            labels (list, optional): List of labels for the document

        Returns:
            dict: Created blueprint details or None if error
        """
        try:
            if schema is None:
                raise ValueError(
                    "Schema cannot be None. Please provide a valid schema."
                )
            # Print schema for debugging
            logger.info(f"Schema: {json.dumps(schema, indent=2)}")

            # Create the blueprint
            response = self.bedrock_client.create_blueprint(
                blueprintName=blueprint_name,
                type=document_type,
                blueprintStage="LIVE",
                schema=schema,
            )
            blueprint_response = response["blueprint"]
            if blueprint_response is None:
                raise ValueError(
                    "Blueprint creation failed. No blueprint response received."
                )

            logger.info(
                f"Blueprint created successfully: {blueprint_response['blueprintArn']}"
            )
            return {"status": "success", "blueprint": blueprint_response}
        except ClientError as e:
            logger.error(f"Error creating BDA blueprint: {e}")
            raise e
        except Exception as e:
            logger.error(f"Error creating blueprint: {e}")
            raise e

    def create_blueprint_version(self, blueprint_arn, project_arn):
        """
        Create a version of a Bedrock Document Analysis blueprint.

        Args:
            blueprint_name (str): Name of the blueprint
            schema (dict): Schema for the blueprint

        Returns:
            dict: Created blueprint version details or None if error
        """
        try:
            response = self.bedrock_client.create_blueprint_version(
                blueprintArn=blueprint_arn
            )
            blueprint_response = response["blueprint"]
            if blueprint_response is None:
                raise ValueError(
                    "Blueprint version creation failed. No blueprint response received."
                )

            self.update_data_automation_project(project_arn, blueprint_response)

            logger.info(
                f"Blueprint version created successfully: {blueprint_response['blueprintArn']}"
            )
            return {"status": "success", "blueprint": blueprint_response}
        except ClientError as e:
            logger.error(f"Error creating BDA blueprint version: {e}")
            raise e
        except Exception as e:
            logger.error(f"Error creating blueprint version: {e}")
            raise e

    def create_blueprint_version_without_project_update(self, blueprint_arn):
        """
        Create a version of a Bedrock Document Analysis blueprint without updating the project.
        This is used in parallel processing to avoid race conditions.

        Args:
            blueprint_arn (str): ARN of the blueprint

        Returns:
            dict: Created blueprint version details or None if error
        """
        try:
            response = self.bedrock_client.create_blueprint_version(
                blueprintArn=blueprint_arn
            )
            blueprint_response = response["blueprint"]
            if blueprint_response is None:
                raise ValueError(
                    "Blueprint version creation failed. No blueprint response received."
                )

            logger.info(
                f"Blueprint version created successfully: {blueprint_response['blueprintArn']}"
            )
            return {"status": "success", "blueprint": blueprint_response}
        except ClientError as e:
            logger.error(f"Error creating BDA blueprint version: {e}")
            raise e
        except Exception as e:
            logger.error(f"Error creating blueprint version: {e}")
            raise e

    def bulk_update_data_automation_project(self, projectArn: str, blueprints: list):
        """
        Update an existing Bedrock Data Automation project with multiple blueprints at once.
        This method is thread-safe and should be called after all parallel blueprint processing is complete.

        Args:
            projectArn (str): ARN of the project to update
            blueprints (list): List of blueprint dicts with blueprintArn and blueprintVersion

        Returns:
            dict: Updated project details or None if error
        """
        try:
            logger.info(f"Bulk updating project with {len(blueprints)} blueprints")

            # Get current project state
            project = self.bedrock_client.get_data_automation_project(
                projectArn=projectArn, projectStage="LIVE"
            )
            project = project.get("project", None)

            customOutputConfiguration = project.get("customOutputConfiguration", None)
            if customOutputConfiguration is None:
                customOutputConfiguration = {"blueprints": []}
                project["customOutputConfiguration"] = customOutputConfiguration

            existing_blueprints = customOutputConfiguration.get("blueprints", [])

            # Create a map of existing blueprints by ARN for quick lookup
            existing_map = {bp.get("blueprintArn"): bp for bp in existing_blueprints}

            # Update or add new blueprints
            for blueprint in blueprints:
                blueprint_arn = blueprint.get("blueprintArn")
                _blueprint = {
                    "blueprintArn": blueprint_arn,
                }
                if blueprint.get("blueprintStage"):
                    _blueprint["blueprintStage"] = blueprint.get("blueprintStage")
                if blueprint.get("blueprintVersion"):
                    _blueprint["blueprintVersion"] = blueprint.get("blueprintVersion")

                # Update existing or add new
                existing_map[blueprint_arn] = _blueprint

            # Convert map back to list
            updated_blueprints = list(existing_map.values())
            customOutputConfiguration["blueprints"] = updated_blueprints

            logger.info(f"Updating data automation project: {projectArn}")
            self.bedrock_client.update_data_automation_project(
                projectArn=projectArn,
                projectDescription=project.get("projectDescription"),
                projectStage=project.get("projectStage"),
                customOutputConfiguration=customOutputConfiguration,
                standardOutputConfiguration=project.get(
                    "standardOutputConfiguration", None
                ),
                overrideConfiguration=project.get("overrideConfiguration", None),
            )
            logger.info(
                f"Successfully bulk updated data automation project: {projectArn}"
            )
            return {"status": "success", "blueprints_count": len(updated_blueprints)}
        except ClientError as e:
            logger.error(f"Failed to bulk update data automation project: {e}")
            raise e
        except Exception as e:
            logger.error(f"Error bulk updating project: {e}")
            raise e

    def update_blueprint(self, blueprint_arn, stage, schema):
        """
        Update a Bedrock Document Analysis blueprint.

        Args:
            blueprint_name (str): Name of the blueprint
            schema (dict): Updated schema for the blueprint

        Returns:
            dict: Updated blueprint details or None if error
        """
        try:
            """
            version_response = self.bedrock_client.create_blueprint_version(
                blueprintArn=blueprint_arn
            )
            if "blueprint" not in version_response:
                raise ValueError("Blueprint update failed. No blueprint response received.")
            new_blueprint = version_response["blueprint"]
            version = new_blueprint.get("blueprintVersion", None)
            """
            response = self.bedrock_client.update_blueprint(
                blueprintArn=blueprint_arn, blueprintStage=stage, schema=schema
            )
            blueprint_response = response["blueprint"]
            if blueprint_response is None:
                raise ValueError(
                    "Blueprint update failed. No blueprint response received."
                )

            logger.info(
                f"Blueprint updated successfully: {blueprint_response['blueprintArn']}"
            )
            return {"status": "success", "blueprint": blueprint_response}
        except ClientError as e:
            logger.error(f"Error Updating BDA blueprint: {e}")
            raise e
        except Exception as e:
            logger.error(f"Error updating blueprint: {e}")
            raise e

    def get_blueprint(self, blueprint_arn, stage):
        """
        Update a Bedrock Document Analysis blueprint.

        Args:
            blueprint_name (str): Name of the blueprint
            schema (dict): Updated schema for the blueprint

        Returns:
            dict: Updated blueprint details or None if error
        """
        try:
            response = self.bedrock_client.get_blueprint(
                blueprintArn=blueprint_arn, blueprintStage=stage
            )
            blueprint_response = response["blueprint"]
            if blueprint_response is None:
                raise ValueError(
                    "Blueprint update failed. No blueprint response received."
                )

            logger.info(
                f"Blueprint updated successfully: {blueprint_response['blueprintArn']}"
            )
            return {"status": "success", "blueprint": blueprint_response}
        except ClientError as e:
            logger.error(f"Error Updating BDA blueprint: {e}")
            raise e
        except Exception as e:
            logger.error(f"Error updating blueprint: {e}")
            raise e

    def list_blueprints(self, projectArn, projectStage):
        try:
            project = self.bedrock_client.get_data_automation_project(
                projectArn=projectArn, projectStage="LIVE"
            )
            project = project.get("project", None)
            customOutputConfiguration = project.get("customOutputConfiguration", None)

            return customOutputConfiguration

        except Exception as e:
            logger.error(f"Error updating blueprint: {e}")
            raise e

    def delete_blueprint(self, blueprint_arn, blueprint_version):
        """
        Delete a BDA blueprint and all its versions.

        BDA requires all versions to be deleted before the base blueprint can be deleted.

        Args:
            blueprint_arn: ARN of the blueprint to delete
            blueprint_version: Current/latest version number of the blueprint

        Returns:
            bool: True if deletion succeeded, False otherwise
        """
        try:
            # Delete all versions including the current one (range is exclusive of end)
            for version in range(1, int(blueprint_version) + 1):
                try:
                    logger.info(
                        f"Deleting blueprint version {version}: {blueprint_arn}"
                    )
                    self.bedrock_client.delete_blueprint(
                        blueprintArn=blueprint_arn, blueprintVersion=str(version)
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to delete version {version} of {blueprint_arn}: {e}"
                    )
                    continue
        except Exception as e:
            logger.error(f"Error iterating versions for {blueprint_arn}: {e}")
            return False

        # Delete the base blueprint (no version specified)
        try:
            logger.info(f"Deleting base blueprint: {blueprint_arn}")
            self.bedrock_client.delete_blueprint(blueprintArn=blueprint_arn)
            logger.info(f"Successfully deleted blueprint: {blueprint_arn}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete base blueprint {blueprint_arn}: {e}")
            return False

    def list_all_blueprints_with_prefix(self, prefix: str):
        """
        List all custom blueprints that start with the given prefix.
        Uses pagination to get all blueprints.

        Args:
            prefix: Blueprint name prefix to filter by

        Returns:
            list: List of blueprint dicts with blueprintArn, blueprintName, blueprintVersion
        """
        blueprints = []
        try:
            paginator = self.bedrock_client.get_paginator("list_blueprints")
            for page in paginator.paginate(blueprintStage="LIVE"):
                for blueprint in page.get("blueprints", []):
                    blueprint_name = blueprint.get("blueprintName", "")
                    # Skip AWS standard blueprints
                    if "aws:blueprint" in blueprint.get("blueprintArn", ""):
                        continue
                    # Filter by prefix
                    if blueprint_name.startswith(prefix):
                        blueprints.append(blueprint)
            logger.info(f"Found {len(blueprints)} blueprints with prefix '{prefix}'")
        except Exception as e:
            logger.error(f"Error listing blueprints with prefix '{prefix}': {e}")
        return blueprints
