// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { EVALUATION_METHOD_OPTIONS, TYPE_ARRAY, TYPE_OBJECT, TYPE_STRING } from '../../../constants/schemaConstants';
import { AttributeLike, ClassLike, resolveAttributeType } from './schemaHelpers';

/**
 * Which evaluation methods the attribute inspector offers for a property.
 *
 * The list is filtered by the property's JSON Schema `type`, and a property
 * written as a bare `{"$ref": "#/$defs/Address"}` has no `type` of its own — so
 * the filter compared `undefined` against every method's `validFor` list, matched
 * nothing, and the Evaluation Method dropdown rendered *empty*: the field could
 * not be given an evaluation method from the UI at all, with nothing on screen
 * saying why (GitHub #906).
 *
 * That shape is not exotic. The builder's own "add attribute referencing an
 * existing class" path (`SchemaBuilder.tsx`) writes `$ref` and clears `type`,
 * while the inspector's "Reference Existing Class" picker keeps `type: 'object'`
 * beside it — so two routes to the same field disagreed, which is what made the
 * bug look intermittent.
 *
 * The backend was never affected: Stickler's `JsonSchemaFieldConverter` resolves
 * `$ref` natively, so a method set by hand-editing the schema JSON is honored at
 * evaluation time. This is a UI-only gap.
 */

/**
 * Is this property a list of objects (`List[Object]`)?
 *
 * Checks BOTH inline object items AND `$ref` items, matching backend logic —
 * only these support HUNGARIAN item matching.
 */
export const isStructuredArrayAttribute = (attribute: AttributeLike | null | undefined): boolean => {
  if (!attribute || attribute.type !== TYPE_ARRAY) return false;
  const items = attribute.items as { type?: unknown; $ref?: unknown } | undefined;
  return items?.type === TYPE_OBJECT || typeof items?.$ref === 'string';
};

/**
 * The evaluation methods valid for this property, with a bare `$ref` resolved to
 * its target type first.
 *
 * Never empty. If the filter eliminates everything — because the type could not
 * be resolved at all, or is one no method declares support for — the unfiltered
 * list is returned instead. An unexplained empty dropdown is the worst outcome
 * available: it removes the only way to configure the field, whereas an
 * over-broad list still lets the user choose, and the choice is checked at
 * evaluation time.
 */
export const availableEvaluationMethods = (
  attribute: AttributeLike | null | undefined,
  availableClasses?: ReadonlyArray<ClassLike>,
): typeof EVALUATION_METHOD_OPTIONS => {
  const resolvedType = resolveAttributeType(attribute, availableClasses);
  const isStructuredArray = isStructuredArrayAttribute(attribute);

  const filtered = EVALUATION_METHOD_OPTIONS.filter((opt) => {
    // HUNGARIAN requires a structured array
    if (opt.requiresStructuredItems) {
      return isStructuredArray;
    }
    if (opt.validFor) {
      // Arrays with SIMPLE items (Array[String], Array[Number], …) are judged on
      // the ITEM type
      if (resolvedType === TYPE_ARRAY && !isStructuredArray) {
        const itemType = (attribute?.items as { type?: string } | undefined)?.type || TYPE_STRING;
        return opt.validFor.includes(itemType);
      }
      // Structured arrays (Array[Object]) are judged on 'array'
      if (resolvedType === TYPE_ARRAY && isStructuredArray) {
        return opt.validFor.includes(TYPE_ARRAY);
      }
      // Everything else: compare the resolved type directly
      return resolvedType !== undefined && opt.validFor.includes(resolvedType);
    }
    // Default: allow for non-structured-arrays
    return !isStructuredArray;
  });

  return filtered.length > 0 ? filtered : EVALUATION_METHOD_OPTIONS;
};
