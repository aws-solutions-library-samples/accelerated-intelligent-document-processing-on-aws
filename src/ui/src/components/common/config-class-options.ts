// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The document classes a configuration profile defines, as dropdown options.
 *
 * Shared by human review and Test Studio annotation: both correct a misclassified
 * section, and both must offer exactly the classes the bound config knows about,
 * because a class the config has no schema for cannot be extracted against.
 */

export interface ConfigClassOption {
  label: string;
  value: string;
  description?: string;
}

interface ConfigWithClasses {
  classes?: unknown;
}

/**
 * Handles both config shapes in the wild: JSON Schema classes name themselves with
 * `$id` or `x-aws-idp-document-type`, pre-migration ones with `name`.
 */
export const getConfigClassOptions = (config?: ConfigWithClasses | null): ConfigClassOption[] => {
  const classes = config?.classes;
  if (!Array.isArray(classes)) return [];
  return (classes as Record<string, unknown>[])
    .map((cls) => {
      const className = String(cls.$id || cls['x-aws-idp-document-type'] || cls.name || '');
      const description = typeof cls.description === 'string' ? cls.description.trim() : '';
      return { label: className, value: className, description: description || undefined };
    })
    .filter((option) => option.value);
};

export default getConfigClassOptions;
