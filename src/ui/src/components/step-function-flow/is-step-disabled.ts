// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Whether a step of an execution was switched off in the configuration the document was
// processed with. Such a step is still entered by the workflow and does no work, so the
// step viewer greys it out. The viewer, its diagram and its details panel share this one
// answer.

/** The parts of a merged configuration that switch a processing step on or off. */
export interface StepConfig {
  summarization?: { enabled?: boolean };
  // v0.6: confidence enablement lives under extraction.confidence; the slim
  // assessment carrier still exposes `enabled` for the step itself.
  extraction?: { confidence?: { enabled?: boolean } };
  assessment?: { enabled?: boolean };
  evaluation?: { enabled?: boolean };
  // Policy rule validation runs when it is not switched off and the configuration has a policy.
  rule_validation?: { enabled?: boolean };
  policy_classes?: unknown[];
  // Review Routing.
  hitl?: { enabled?: boolean };
}

/** What the state names of the policy rule validation step start with (PolicyClassificationStep and the two after it). */
const POLICY_STATE_PREFIX = 'policy';
/** The state that does Review Routing. */
const ROUTING_STATE = 'routingstep';

/** True when the configuration switches off the step a state of this name does. */
export const isStepDisabled = (stepName: string, config: StepConfig | null | undefined): boolean => {
  if (!config) return false;

  const stepNameLower = stepName.toLowerCase();
  // A state name as the workflow has it, however the viewer spaces it.
  const stateName = stepNameLower.replace(/\s+/g, '');

  // The policy rule validation states: switched off, or with no policy to validate against
  if (stateName.startsWith(POLICY_STATE_PREFIX)) {
    return config.rule_validation?.enabled === false || (Array.isArray(config.policy_classes) && config.policy_classes.length === 0);
  }

  // Review Routing
  if (stateName === ROUTING_STATE) {
    return config.hitl?.enabled === false;
  }

  // Check if this is a summarization step
  if (stepNameLower.includes('summarization') || stepNameLower.includes('summary')) {
    return config.summarization?.enabled === false;
  }

  // Check if this is an assessment step
  if (stepNameLower.includes('assessment') || stepNameLower.includes('assess')) {
    return (config.extraction?.confidence?.enabled ?? config.assessment?.enabled) === false;
  }

  // Check if this is an evaluation step
  if (stepNameLower.includes('evaluation') || stepNameLower.includes('evaluate')) {
    return config.evaluation?.enabled === false;
  }

  return false;
};
