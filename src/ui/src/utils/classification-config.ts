// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Classification config (format 0.9) keeps every model setting inside the call
 * that uses it. These helpers answer "which call assigns the class?" in one
 * place, mirroring `class_assigning_llm_path` in
 * `lib/idp_common_pkg/idp_common/config/models.py`.
 */

/** Path under `classification` of the call that assigns the class, per strategy. */
export const CLASS_ASSIGNING_LLM_PATHS: Record<string, readonly string[]> = {
  single_pass: ['one_stage', 'llm'],
  two_stage: ['two_stage', 'section_classification', 'llm'],
};

/** The class-assigning call's path for a classification block; `single_pass` by default. */
export const classAssigningLlmPath = (classification: unknown): readonly string[] => {
  const strategy =
    classification && typeof classification === 'object'
      ? String((classification as Record<string, unknown>).strategy ?? '')
          .trim()
          .toLowerCase()
      : '';
  return CLASS_ASSIGNING_LLM_PATHS[strategy] ?? CLASS_ASSIGNING_LLM_PATHS.single_pass;
};

/** The model that assigns the class under the active strategy, or undefined. */
export const classificationModelOf = (classification: unknown): string | undefined => {
  let node: unknown = classification;
  for (const key of classAssigningLlmPath(classification)) {
    node = node && typeof node === 'object' ? (node as Record<string, unknown>)[key] : undefined;
  }
  const model = node && typeof node === 'object' ? (node as Record<string, unknown>).model : undefined;
  return typeof model === 'string' && model ? model : undefined;
};

/**
 * Write `model` into the class-assigning call of `classification`, creating the
 * path as needed. Segmentation keeps its own model: it only finds boundaries.
 */
export const setClassificationModel = (classification: Record<string, unknown>, model: string): void => {
  let node = classification;
  for (const key of classAssigningLlmPath(classification)) {
    const child = node[key];
    if (!child || typeof child !== 'object' || Array.isArray(child)) {
      node[key] = {};
    }
    node = node[key] as Record<string, unknown>;
  }
  node.model = model;
};
